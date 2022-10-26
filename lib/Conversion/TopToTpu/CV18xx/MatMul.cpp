//===----------------------------------------------------------------------===//
//
// Copyright (c) 2020-2030 by Sophgo Technologies Inc. All rights reserved.
//
// Licensed under the Apache License v2.0.
// See http://www.apache.org/licenses/LICENSE-2.0 for license information.
// SPDX-License-Identifier: Apache-2.0
//
//===----------------------------------------------------------------------===//

#include "tpu_mlir/Conversion/TopToTpu/LoweringCV18xx.h"
#include "llvm/Support/Debug.h"

#define DEBUG_TYPE "lowering-matmul"

namespace tpu_mlir {
namespace cv18xx {
static void quantizeWeightInt8ForFC(float *filter, float *bias, int64_t batch,
                                    int64_t N, int64_t K, double threshold_y,
                                    double threshold_x, int8_t *new_filter,
                                    int32_t *new_bias,
                                    int64_t *rshift_per_batch,
                                    int64_t *multiplier_per_batch) {

  // find qscale
  std::vector<float> max_filter(batch);
  std::vector<double> qscale(batch);
  int64_t isz = N * K;
  for (int i = 0; i < batch; i++) {
    max_filter[i] = findMaxabs<float>(filter + i * isz, isz);
    qscale[i] = getQscaleForFilter(max_filter[i], threshold_y, threshold_x);
  }

  std::vector<float> max_bias(batch * N);
  if (bias) {
    for (int i = 0; i < batch; i++) {
      for (int n = 0; n < N; ++n) {
        int index = i * N + n;
        max_bias[index] = fabs(bias[index]);
        double qscale_bias = getQscaleForBias(max_bias[index], threshold_y);
        if (qscale_bias > qscale[i]) {
          LLVM_DEBUG(llvm::errs()
                         << "WARNING: adjust qscale for bias"
                         << ", qscale_filter = " << qscale[i]
                         << ", qscale_bias = " << qscale_bias << "\n";);
          qscale[i] = qscale_bias;
        }
      }
    }
  }
  // decompose qscale into rshift and muliplier
  int64_t multiplier;
  int64_t rshift;
  for (int i = 0; i < batch; i++) {
    getRShiftAndMultiplierFromQScale(qscale[i], multiplier_per_batch + i,
                                     rshift_per_batch + i, true, 255);
  }

  for (int i = 0; i < batch; i++) {
    int index = i * isz;
    quantizeFilterRShiftAndMultiplier(
        filter + index, new_filter + index, isz, threshold_y, threshold_x,
        rshift_per_batch[i], multiplier_per_batch[i], true);
  }

  if (bias) {
    for (int i = 0; i < batch; i++) {
      int index = i * N;
      quantizeBiasRShiftAndMultiplier(bias + index, new_bias + index, N,
                                      threshold_y, rshift_per_batch[i],
                                      multiplier_per_batch[i], true);
    }
  }
}

void MatMulLowering::LoweringINT8(PatternRewriter &rewriter, top::MatMulOp op,
                                  bool asymmetric) const {
  std::vector<Value> operands;
  std::vector<NamedAttribute> attrs;
  int64_t batch, M, K, N;
  bool with_bias, relu;
  double relu_limit;
  op.parseParam(batch, M, K, N, with_bias, relu, relu_limit);
  auto th_output = Quant::getThreshold(op.output());
  auto th_input = Quant::getThreshold(op.input());
  std::vector<int64_t> multipliers;
  std::vector<int64_t> rshifts;
  mlir::Value right_operand = op.right();
  mlir::Value bias_operand = op.bias();
  bool is_fc = isa<top::WeightOp>(op.right().getDefiningOp());
  if (is_fc) {
    // fc
    auto rightOp = cast<top::WeightOp>(op.right().getDefiningOp());
    auto right_f32 = rightOp.read<float>();
    assert(right_f32->size() == K * N);
    auto right_i8 = std::vector<int8_t>(right_f32->size());

    std::shared_ptr<std::vector<float>> bias_f32;
    std::vector<int32_t> bias_i32;
    if (with_bias) {
      auto biasOp = cast<top::WeightOp>(op.bias().getDefiningOp());
      bias_f32 = biasOp.read<float>();
      bias_i32.resize(bias_f32->size());
    }

    multipliers.resize(batch);
    rshifts.resize(batch);
    quantizeWeightInt8ForFC(right_f32->data(),
                            with_bias ? bias_f32->data() : nullptr, batch, N, K,
                            th_output, th_input, right_i8.data(),
                            with_bias ? bias_i32.data() : nullptr,
                            rshifts.data(), multipliers.data());

    auto right_type = op.right().getType().cast<RankedTensorType>();
    auto new_right_type = RankedTensorType::get(
        right_type.getShape(), rewriter.getIntegerType(8, true));
    right_operand =
        top::WeightOp::create(op, "filter_i8", right_i8, new_right_type);
    if (with_bias) {
      auto bias_type = op.bias().getType().cast<RankedTensorType>();
      auto new_type = RankedTensorType::get(bias_type.getShape(),
                                            rewriter.getIntegerType(32, true));
      bias_operand =
          top::WeightOp::create(op, "bias_int32", bias_i32, new_type);
    }
    for (auto &attr : op->getAttrs()) {
      attrs.emplace_back(attr);
    }
  } else {
    // matmul
    auto th_right = Quant::getThreshold(op.right());
    auto th_prod = th_right * th_input;
    auto qscale = th_prod / th_output / 127.0;
    multipliers.resize(1);
    rshifts.resize(1);
    if (std::abs(qscale - 1.0) > 1e-5) {
      getRShiftAndMultiplierFromQScale(qscale, multipliers.data(),
                                       rshifts.data(), true);
    }
  }
  auto ctx = op->getContext();
  attrs.push_back(rewriter.getNamedAttr(
      "quant_mode", tpu::RequantModeAttr::get(ctx, tpu::RequantMode::Normal)));
  attrs.push_back(rewriter.getNamedAttr(
      "rshifts", rewriter.getI64ArrayAttr(ArrayRef<int64_t>{rshifts})));
  attrs.push_back(rewriter.getNamedAttr(
      "multipliers", rewriter.getI64ArrayAttr(ArrayRef<int64_t>{multipliers})));
  operands.emplace_back(op.input());
  operands.emplace_back(right_operand);
  operands.emplace_back(bias_operand);

  auto newType = Quant::getQuantInt8Type(op.output(), asymmetric);
  rewriter.replaceOpWithNewOp<tpu::MatMulOp>(op, newType, operands, attrs);
  return;
}

void MatMulLowering::LoweringBF16(PatternRewriter &rewriter,
                                  top::MatMulOp op) const {
  llvm_unreachable("Not supported now");
}

} // namespace cv18xx
} // namespace tpu_mlir