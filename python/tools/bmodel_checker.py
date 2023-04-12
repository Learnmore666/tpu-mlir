#!/usr/bin/env python3
# ==============================================================================
#
# Copyright (C) 2022 Sophgo Technologies Inc.  All rights reserved.
#
# TPU-MLIR is licensed under the 2-Clause BSD License except for the
# third-party components.
#
# ==============================================================================

import json, re
from enum import Enum
from dataclasses import dataclass
from collections import namedtuple, OrderedDict
import argparse
import numpy as np
from rich.console import Console

from numpy_helper.tensor_compare import TensorCompare
from utils.bmodel_dis.opdef_1684x import bdc_base, dma_base
import utils.bmodel_dis.opparam_1684x as opparam
import bmodel_dis
from tdb import Tdb


# ------------------------------------------------------------------------------
# this can alter the print style
console = Console()
# ------------------------------------------------------------------------------


@dataclass
class Value:
    value: dict
    index: int


class Result(Value):
    pass


class Operand(Value):
    pass


class TensorLoc:
    __solts__ = ("tensor_loc", "cmd_index")
    # so many namedTuple, could we use a smart Table?
    ID = namedtuple("ID", ["sunnetId", "bdc", "gdma"])
    TR = namedtuple("TensorRecord", ["tensor", "record"])

    def __init__(self, ts_des_file):
        self.cmd_index = {}

        def tuples_hook(pairs):
            """
            Convert lists to tuples in JSON objects. Not works in root object.
            """
            return {k: tuple(v) if isinstance(v, list) else v for k, v in pairs}

        with open(ts_des_file, "r") as f:
            self.tensor_loc = json.load(f, object_pairs_hook=tuples_hook)

        # breakpoint -> (tensors, record)*
        self.breakpoint_loc = OrderedDict()
        for loc in self.tensor_loc:
            loc_s = {k: v for k, v in loc.items() if k not in ("operands", "results")}
            # before this nodechip command, we can check the input data.
            key = self.ID(loc["subnet_id"], *loc["bdc_gdma_id(before)"])
            self.breakpoint_loc.setdefault(key, []).extend(
                self.TR(Operand(v, i), loc_s)
                for i, v in enumerate(loc["operands"])
                if v != {}
            )

            # after this nodechip command, we can check the output data.
            key = self.ID(loc["subnet_id"], *loc["bdc_gdma_id(after)"])
            self.breakpoint_loc.setdefault(key, []).extend(
                self.TR(Result(v, i), loc_s)
                for i, v in enumerate(loc["results"])
                if v != {}
            )

    def merge_cmd(self, bdc, gdma, subnet_id):
        cmd = bmodel_dis.merge_cmd(bdc, gdma)
        for i, x in enumerate(cmd):
            if isinstance(x, bdc_base):
                k = self.ID(subnet_id, x.cmd_id, None)
            else:
                k = self.ID(subnet_id, None, x.cmd_id)
            self.cmd_index[k] = i
        return cmd

    def merge_cmd_1(self, bdc, gdma, subnet_id):
        """sort commands as nodechip"""
        from itertools import tee

        bp = filter(lambda x: x[0] == subnet_id, self.breakpoint_loc.keys())
        cmd = []

        def pairwise(iterable):
            # pairwise('ABCDEFG') --> AB BC CD DE EF FG
            a, b = tee(iterable)
            next(b, None)
            return zip(a, b)

        for a, b in pairwise(bp):
            nc_cmd = bmodel_dis.merge_cmd(bdc[a.bdc : b.bdc], gdma[a.gdma : b.gdma])
            cmd.extend(nc_cmd)
        return cmd

    def set_breakpoint(self, tdb: Tdb, subnet_id: int = 0):
        bp = filter(lambda x: x[0] == subnet_id, self.breakpoint_loc.keys())

        def bb(bdc_id, gdma_id):
            x, y = -1, -1
            if bdc_id > 0:
                x = self.cmd_index[(subnet_id, bdc_id, None)]
            if gdma_id > 0:
                y = self.cmd_index[(subnet_id, None, gdma_id)]
            return max(x, y)

        tdb.breakpoint.extend(bb(x, y) for _, x, y in bp)


to_dtype = {
    "f32": opparam.DType.f32,
    "f16": opparam.DType.f16,
    "bf16": opparam.DType.bf16,
    "i8": opparam.DType.i8,
    "si8": opparam.DType.si8,
    "ui8": opparam.DType.ui8,
    "u8": opparam.DType.ui8,
    "i16": opparam.DType.i16,
    "si16": opparam.DType.si16,
    "ui16": opparam.DType.ui16,
    "u16": opparam.DType.ui16,
    "i32": opparam.DType.i32,
    "si32": opparam.DType.si32,
    "ui32": opparam.DType.ui32,
    "u32": opparam.DType.ui32,
}


def get_shape_and_dtype(input_string: str):
    pattern = r"<(\d+(?:x\d+)*)x(\S+)>"
    matches = re.findall(pattern, input_string)
    shape = []
    dtype = []
    for match in matches:
        shape.extend(map(int, match[0].split("x")))
        dtype.append(match[1])

    dtype = to_dtype[dtype[0]]
    return shape, dtype


def get_mlir_type_info(mlir_type: str):
    ut = namedtuple("TensorType", ["shape", "type", "scale", "zero_point"])

    shape = r"(?P<shape>\d+(x\d+)*)"
    quant = r"!quant\.uniform"
    storage_type = r"(?P<storage_type>\w+)"
    express_type = r"(?P<express_type>\w+)"
    scale = r"(?P<scale>[-+]?\d*\.\d+([eE][-+]?\d+)?|\d+)"
    min_r = r"(?P<min>[-+]?\d*\.\d+([eE][-+]?\d+)?|\d+)"
    max_r = r"(?P<max>[-+]?\d*\.\d+([eE][-+]?\d+)?|\d+)"
    zero_point = r"(?P<zero_point>[-+]?\d+)"
    address = r"(?P<address>\d+)\ :\ \w+"

    def match_type():
        pattern = rf"tensor<{shape}x{storage_type}(, {address})?>"
        match = re.search(pattern, mlir_type)
        if match:
            return match
        pattern = rf"tensor<{shape}x{quant}<{storage_type}:{express_type},\ {scale}(:{zero_point})?>(,\ {address})?>"
        match = re.search(pattern, mlir_type)
        if match:
            return match
        cali = r"!quant.calibrated"
        pattern = (
            rf"tensor<{shape}x{cali}<{storage_type}<{min_r}:{max_r}>>(, {address})?>"
        )
        match = re.search(pattern, mlir_type)
        if match:
            return match
        raise ValueError(f"Do not recognize type: {mlir_type}")

    match = match_type()
    zp = 0
    if "zero_point" in match.groupdict():
        zp = match.group("zero_point")
        if zp != None:
            zp = int(zp)
        else:
            zp = 0
    sc = 1
    if "scale" in match.groupdict():
        sc = match.group("scale")
        if sc != None:
            sc = float(sc)
        else:
            sc = 1

    return ut(
        [int(x) for x in match.group("shape").split("x")],
        to_dtype[match.group("storage_type")],
        sc,
        zp,
    )


class TensorBuilder:
    _slot_ = "tensor_des"

    def __init__(self, tensor_des):
        address = tensor_des["address"]

        shape, dtype = get_shape_and_dtype(tensor_des["memory_type"])
        layout = tensor_des["layout"]
        layout = {
            "eu_align": opparam.Layout.alignEU,
            "eu_align_group3d": opparam.Layout.alignEU,
            "compact": opparam.Layout.compact,
            "compact_group3d": opparam.Layout.compact,
            "continuous_group3d": None,
            "continuous": None,
        }[layout]

        # local memory
        if address < int("0x1000000", 16):
            address += opparam.memmap[opparam.MType.R][0]
        self.name = tensor_des["name"]
        stride = None

        # global memory
        if layout == None:
            # lib/Dialect/Tpu/Interfaces/BM1684X/Load.cpp
            # load/store case
            reshape = tensor_des["reshape"]
            if reshape:
                _, c, d, h, w = [int(x) for x in tensor_des["reshape"][1:-1].split("x")]
                _layout = tensor_des["layout"]
                if _layout == "continuous":
                    stride = (c * d * h * w, h * w, w, 1)
                elif _layout == "continuous_group3d":
                    stride = (h * w, d * h * w, w, 1)
                else:
                    raise ValueError(f"Not supported layout: {_layout}")
            else:
                # global layer
                stride = tuple(np.cumprod([1] + shape[-1:0:-1])[::-1])

        self.memref = opparam.MemRef(
            address, shape, dtype, layout=layout, stride=stride
        )
        self.tensor_des = tensor_des
        self.mlir_type = get_mlir_type_info(tensor_des["type"])

    def to_f32data(self, data):
        zp = self.mlir_type.zero_point
        scale = self.mlir_type.scale
        return ((data).astype(np.float32) - zp) * scale

    def get_ref_data(self, refence_data):
        reshape = self.tensor_des["reshape"]
        ref_data = refence_data
        if reshape:
            reshape = reshape[1:-1].replace("x", ",")
            ref_data = eval(f"ref_data.reshape({reshape})")
        _slice = self.tensor_des["slice"]
        data = eval(f"ref_data{_slice}")
        # The data in HW has a transposed collapsed shape.
        # To align the Bmodel with TPU.mlir, we need to transpose the reference data.
        if self.tensor_des["layout"] in (
            "continuous_group3d",
            "eu_align_group3d",
            "compact_group3d",
        ):
            n, c, d, h, w = 0, 1, 2, 3, 4
            data = data.transpose((d, n, c, h, w))
        return data


class StateMsg:
    __solts__ = ("msg", "state")

    def __init__(self, state=None, msg=None):
        self.state = state
        self.msg = msg

    def __bool__(self):
        return bool(self.state)

    def __eq__(self, other):
        if isinstance(other, StateMsg):
            return self.state == other.state
        if other not in (None, False, True):
            return False
        return self.state == other

    def __repr__(self):
        if self.msg == None:
            return str(self.state)
        return str(self.msg)

    def __rich__(self):
        return self.msg


@dataclass
class ErrorMsg:
    @dataclass
    class Info:
        numpy_err: str
        value: str
        codes: str
        opcode: str
        bdc_gdma_id_before: list
        bdc_gdma_id_after: list

        def __rich_console__(self, console, options):
            from rich.json import JSON
            from rich.panel import Panel
            from rich.table import Table
            from rich import box

            table = Table(
                title="value-Info",
                show_lines=True,
                show_header=False,
                box=box.HORIZONTALS,
            )
            table.add_row("opcode", self.opcode)
            table.add_row("value", JSON(self.value))
            table.add_row("bdc_gdma_id (before codegen)", str(self.bdc_gdma_id_before))
            table.add_row("bdc_gdma_id (after codegen)", str(self.bdc_gdma_id_after))
            yield table
            yield Panel(self.numpy_err, title="data-error", style="red")
            yield Panel(self.codes, title="asm")

    file_line: int
    info: Info

    def __rich__(self):
        from rich.panel import Panel

        return Panel(
            self.info,
            title=f"[b magenta] file-line:[/b magenta] #{self.file_line}",
            title_align="left",
            expand=False,
        )


class DataChecker(TensorCompare):
    """
    Check, Record
    """

    TS = namedtuple("TensorState", ["state", "close", "loop", "metric", "details"])

    def __init__(self):
        super().__init__()
        self.failed_tensors = OrderedDict()

    def diff_details(self, d1, d2, _):
        # override TensorCompare
        _d1 = d1.ravel()
        _d2 = d2.ravel()
        diff = np.abs(_d1 - _d2)
        k = min(_d1.size, 10)
        idx = np.argpartition(diff, -k)[-k:]
        idx = [
            x[0] for x in sorted(zip(idx, diff[idx]), key=lambda x: x[1], reverse=True)
        ]
        return {"x": _d1[idx], "y": _d2[idx]}

    def close(self, actual, desired):
        result = self.TS(
            *self.compare(actual, desired, verbose=2, int8_tensor_close=True)
        )
        return result.state

    def assert_allclose(self, actual, desired, name):
        from functools import partial
        from numpy.core import array_repr

        result = self.TS(
            *self.compare(actual, desired, verbose=2, int8_tensor_close=True)
        )
        if not result.state:
            self.failed_tensors[name + "_actual"] = actual
            self.failed_tensors[name + "_desired"] = desired

            metric = result.metric
            header = (
                "Not equal to tolerance "
                + f"cos={self.cosine_similarity_tol}"
                + f", euc={self.euclidean_similarity_tol}"
            )
            remarks = [
                f"cosine similarity: {metric['cosine']:.6f}",
                f"euclidean similarity: {metric['euclid']:.6f}",
            ]
            msg = ["\n" + header, "", "\n".join(remarks)]
            r_func = partial(array_repr, precision=6)
            msg.extend(
                f" {n}: {r_func(r.astype(float))[6:-1]}"
                for n, r in result.details.items()
            )
            try:
                np.testing.assert_allclose(
                    actual, desired, rtol=1e-3, atol=1e-1, verbose=False
                )
            except AssertionError as e:
                msg.append(str(e))
            raise AssertionError("\n".join(msg))

    def save(self, file_name):
        if self.failed_tensors:
            np.savez(file_name, **self.failed_tensors)


DATA_CHECKER = DataChecker()
ASM_CONTEXT_LENGTH = 2


def check_data(tdb, tensors, ref_data):
    # multiple tensors
    def get_line_info(e, tensor_des):
        info = ("opcode", "bdc_gdma_id(before)", "bdc_gdma_id(after)")
        t = tensor_des.tensor
        t_info = {"loc": f"{t.__class__.__name__}[{t.index}]"}
        t_info.update(tensor_des.tensor.value)
        return ErrorMsg(
            tensor_des.record["file-line"],
            ErrorMsg.Info(
                str(e),
                json.dumps(t_info),
                tdb.get_context(ASM_CONTEXT_LENGTH),
                *(tensor_des.record[x] for x in info),
            ),
        )

    result = []
    for tensor_des in tensors:
        t = TensorBuilder(tensor_des.tensor.value)
        if t.name in ref_data:
            actual = t.to_f32data(tdb.get_data(t.memref))
            desired = t.get_ref_data(ref_data[t.name])
            try:
                actual = actual.reshape(desired.shape)
                name = f"{t.name}_asm_{tdb.current_line}"
                DATA_CHECKER.assert_allclose(actual, desired, name)
            except AssertionError as e:
                result.append(StateMsg(False, get_line_info(e, tensor_des)))
            else:
                result.append(StateMsg(True))
        else:
            result.append(StateMsg(None))

    return result


class State(Enum):
    Pass = 0
    Fail = 1
    Unknown = 2


class Checker:
    _bmodel_file = "compilation.bmodel"
    _input_data_file = "input_ref_data.dat"
    _tensor_loc_file = "tensor_location.json"
    markers = {State.Pass: "✓", State.Unknown: "?", State.Fail: "✗"}
    colors = {State.Pass: "green", State.Unknown: "white", State.Fail: "red"}
    SI = namedtuple("SubNetInstruction", ["subnet_id", "instruction_id"])
    LS = namedtuple("LineState", ["line", "operands", "results"])

    def __init__(self, folder, ref_data_npz, fail_fast=False):
        self.tensor_loc = TensorLoc(f"{folder}/{self._tensor_loc_file}")
        self.bmodel_file = f"{folder}/{self._bmodel_file}"
        self.input_data_file = f"{folder}/{self._input_data_file}"
        bmodel_dis.MERGE_CMD_FUN = self.tensor_loc.merge_cmd
        self.ref_data = np.load(ref_data_npz)
        self.state = State.Unknown

        # run checker
        self.check_data(fail_fast)
        self.gen_report()

    def check_data(self, fail_fast=False):
        """
        build: RPS -> OPS
        """
        self.results = {}
        RPS = namedtuple(
            "ReportState", ["line", "subnet_id", "ins_before", "ins_after"]
        )
        OPS = namedtuple("OpState", ["operands_state", "results_state"])

        from rich.progress import track

        tdb = Tdb()
        # disable message
        tdb.enable_message = False
        tdb.load_file(self.bmodel_file)
        tdb.start()
        tdb.load_data(self.input_data_file)
        self.tensor_loc.set_breakpoint(tdb)
        tdb.temporary_breakpoint = True

        for bp, _ in track(
            zip(self.tensor_loc.breakpoint_loc.items(), tdb.continues),
            description="Checking...",
            total=len(self.tensor_loc.breakpoint_loc),
        ):
            # The instruction recorded in TensorLoc represents the
            # data checkpoint after that instruction is executed.
            # At the breakpoint, the current instruction has not yet been executed.
            # To make it take effect, we need to run it explicitly.
            tdb.next()

            _, tensor_record = bp
            vf = check_data(tdb, tensor_record, self.ref_data)

            for st, tr in zip(vf, tensor_record):
                info = (
                    "file-line",
                    "subnet_id",
                    "bdc_gdma_id(before)",
                    "bdc_gdma_id(after)",
                )

                key = RPS(*(tr.record[i] for i in info))
                if isinstance(tr.tensor, Operand):
                    self.results.setdefault(key, OPS([], [])).operands_state.append(st)
                else:
                    self.results.setdefault(key, OPS([], [])).results_state.append(st)

            if False in vf:
                self.state = State.Fail
            if self.state == State.Unknown and True in vf:
                self.state = State.Pass

            if fail_fast and self.state == State.Fail:
                return

    def get_failed_tensor(self):
        for k, v in self.results.items():
            opdres = v.operands_state + v.results_state
            if False in opdres:
                yield k, [x for x in opdres if x == False]
            continue

    def gen_report(self):
        cmd_idx = self.tensor_loc.cmd_index
        self.ins_state = {}

        def state_aggragate(state):
            if state == []:
                return State.Unknown
            if False in state:
                return State.Fail
            if None in state:
                return State.Unknown
            return State.Pass

        for k, v in self.results.items():
            bdc_x, gdma_x = k.ins_before
            bdc_y, gdma_y = k.ins_after
            sid = k.subnet_id
            line = k.line
            ops = state_aggragate(v.operands_state)
            res = state_aggragate(v.results_state)
            self.ins_state.update(
                {
                    self.SI(sid, cmd_idx[(sid, x + 1, None)]): self.LS(line, ops, res)
                    for x in range(bdc_x, bdc_y)
                }
            )
            self.ins_state.update(
                {
                    self.SI(sid, cmd_idx[(sid, None, x + 1)]): self.LS(line, ops, res)
                    for x in range(gdma_x, gdma_y)
                }
            )

    def _get_state(self, state_fun, title, columns=10):
        from rich.table import Table
        from rich import box

        # support partial check
        ins_num = 0
        if self.ins_state:
            ins_num = max(x.instruction_id for x in self.ins_state) + 1

        ins_state = [self.LS("?", State.Unknown, State.Unknown)] * ins_num
        for si, s in self.ins_state.items():
            ins_state[si.instruction_id] = s

        def gen_state():
            for x in range(0, ins_num, columns):
                st = ins_state[x : min(x + columns, ins_num)]
                yield [state_fun(s) for s in st]

        table = Table(
            title=f"[bold]{title}",
            style="bold cyan",
            box=box.SIMPLE_HEAD,
            padding=(0, 0, 0, 0),
        )

        table.add_column("INS", justify="right", style="bold")
        for i in range(columns):
            table.add_column(f"{i:>2}", justify="right", style="bold")

        for i, row in enumerate(gen_state()):
            table.add_row(f"[bold]{i*columns}[/bold]", *row)
        return table

    def get_summary(self, style=""):
        def com(state):
            return f"[{self.colors[state]}]{self.markers[state]:>1}[/]"

        state = com(self.state)

        def simple():
            func = lambda s: com(s.results)
            return self._get_state(func, f"Check-Result[{state}] Summary", 20)

        def full():
            func = lambda s: f"({com(s.operands)}{com(s.results)})"
            return self._get_state(func, f"Check-Operand-Result[{state}] Summary", 10)

        def line():
            func = lambda s: f"[{self.colors[s.results]}]{s.line}[/]"
            return self._get_state(func, f"Check-Line[{state}] Summary", 20)

        def line_full():
            func = lambda s: f"({s.line}{com(s.operands)}{com(s.results)})"
            return self._get_state(
                func, f"Check-Line-Operand-Result[{state}] Summary", 10
            )

        return [simple, line, full, line_full][style.lower().count("v")]()

    def __bool__(self):
        return self.state == State.Pass


def interactive_mode(checker):
    import sys
    import termios
    import tty

    failed_ts = {k: v for k, v in checker.get_failed_tensor()}
    lines = {}
    tensor_msg = []
    for k, v in failed_ts.items():
        if k.line not in lines:
            lines[k.line] = len(tensor_msg)
        tensor_msg.extend(v)

    line_num = list(lines.keys())
    index = -1
    verbose = "vvv"

    def get_char():
        """Get a single character from the user."""
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(sys.stdin.fileno())
            char = sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        return char

    def main_loop():
        nonlocal verbose, index
        try:
            while True:
                user_input = get_char()
                if user_input.lower() == "l":
                    action = input(
                        f"Enter a line number in:\n {line_num}\n(or 'q' to finish): "
                    )
                    if action.lower() == "q":
                        continue
                    else:
                        try:
                            line = int(action)
                            if line in lines:
                                index = lines[line]
                                console.print(tensor_msg[index])
                            else:
                                console.print(f"Invalid line number: {line}.")
                                console.print(f"Only supports {line_num}")
                        except:
                            console.print(
                                f"Invalid inputs {action}, only supports number."
                            )
                elif user_input.lower() == "n" or user_input.lower() == "p":
                    if user_input.lower() == "n":
                        index += 1
                    else:
                        index -= 1
                    if index >= len(tensor_msg):
                        index = 0
                        console.print("Back to the beginning.")
                    if index < 0:
                        console.print("Cycle from the end.")
                        index += len(tensor_msg)
                    console.print(tensor_msg[index])
                    console.print(f"{index+1}/{len(tensor_msg)}")
                elif user_input.lower() == "r":
                    console.print(checker.get_summary(verbose))
                elif user_input.lower() == "v":
                    action = input(f"Enter verbose level (or 'q' to finish): ")
                    if action.lower() == "q":
                        continue
                    else:
                        verbose = "v" * action.count("v")
                elif user_input in ("q", "\x03"):
                    console.print("Exiting...")
                    break
                else:
                    console.print("Invalid choice. Please try again.")

        except KeyboardInterrupt:
            # Handle Ctrl+C to exit gracefully
            console.print("\nExiting...")

    inter = input("Run into interactive mode? (y or yes): ")
    if inter.lower() in ("y", "yes"):
        with console.screen():
            console.print(checker.get_summary("vvv"))
            console.print("n(ext), p(revious), l(ine), v(erbose), r(eport), q(uit)")
            main_loop()


def save_to_file(checker, report_file):
    from datetime import datetime

    with open(report_file, "wt") as rf:
        console = Console(file=rf)
        console.rule(f"Report Generated {datetime.now().ctime()}")
        console.print(checker.get_summary("vvv"))
        for k, v in checker.get_failed_tensor():
            console.rule(f"Line {k.line}")
            for t in v:
                console.print(t)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--context_dir",
        required=True,
        help="The folder should contain the BModel, its input_data, and tensor_location files.",
    )
    parser.add_argument(
        "--reference_data",
        required=True,
        help="The reference data used for checking this BModel.",
    )
    parser.add_argument(
        "--tolerance", default="0.99,0.90", help="tolerance for compare."
    )
    parser.add_argument(
        "--report", type=str, help="The report file for saving state and internal data."
    )
    parser.add_argument(
        "--fail_fast", action="store_true", help="Stop if there is a check failure."
    )
    parser.add_argument(
        "--verbose",
        type=str,
        nargs="?",
        const="",
        help="Control the report information.",
    )
    parser.add_argument("--no_interactive", action="store_true")

    args = parser.parse_args()

    cos_t, euc_t = eval(args.tolerance)
    DATA_CHECKER.cosine_similarity_tol = cos_t
    DATA_CHECKER.euclidean_similarity_tol = euc_t
    DATA_CHECKER.signal_to_quantization_noise_tol = float("-inf")

    checker = Checker(args.context_dir, args.reference_data, args.fail_fast)

    if not args.no_interactive or args.verbose is not None:
        console.print(checker.get_summary("" if args.verbose is None else args.verbose))

    if checker.state == State.Unknown:
        exit(2)

    if not checker:
        if not args.no_interactive:
            interactive_mode(checker)
        if args.report:
            save_to_file(checker, args.report)
            DATA_CHECKER.save(args.report + ".err.npz")
        exit(1)
    exit(0)