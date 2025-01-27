# -*- mode: python -*-
# =============================================================================
#  @@-COPYRIGHT-START-@@
#
#  Copyright (c) 2020-2023, Qualcomm Innovation Center, Inc. All rights reserved.
#
#  Redistribution and use in source and binary forms, with or without
#  modification, are permitted provided that the following conditions are met:
#
#  1. Redistributions of source code must retain the above copyright notice,
#     this list of conditions and the following disclaimer.
#
#  2. Redistributions in binary form must reproduce the above copyright notice,
#     this list of conditions and the following disclaimer in the documentation
#     and/or other materials provided with the distribution.
#
#  3. Neither the name of the copyright holder nor the names of its contributors
#     may be used to endorse or promote products derived from this software
#     without specific prior written permission.
#
#  THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
#  AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
#  IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
#  ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
#  LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
#  CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
#  SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
#  INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
#  CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
#  ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
#  POSSIBILITY OF SUCH DAMAGE.
#
#  SPDX-License-Identifier: BSD-3-Clause
#
#  @@-COPYRIGHT-END-@@
# =============================================================================

""" implements straight through gradient computation for Quantize Op """
from typing import Tuple

import tensorflow as tf

from aimet_common import libpymo
from aimet_tensorflow.defs import AxisHandling
from aimet_tensorflow.utils.constants import QuantizeOpIndices


def _compute_dloss_by_dx(op, grad):
    x = tf.cast(op.inputs[0], tf.float32)
    encoding_min = tf.cast(op.inputs[int(QuantizeOpIndices.encoding_min)], tf.float32)
    encoding_max = tf.cast(op.inputs[int(QuantizeOpIndices.encoding_max)], tf.float32)
    op_mode = tf.cast(op.inputs[int(QuantizeOpIndices.op_mode)], tf.int8)

    inner_cond = tf.compat.v2.where(tf.less_equal(x, encoding_max),  # condition to check per value
                                    1.0,  # execute if true
                                    0.0)  # execute if false

    dloss_by_dx = (tf.compat.v2.where(tf.less_equal(encoding_min, x),  # condition to check per value
                                      inner_cond,  # execute if true
                                      0.0)) * grad

    # Pass through gradient for skipped ops
    dloss_by_dx = tf.cond(tf.equal(op_mode, 3), lambda: grad, lambda: dloss_by_dx)

    return dloss_by_dx


# by default, we will have this registered for Qc Quantize op.
@tf.RegisterGradient("QcQuantize")
def _qc_straight_through_estimator_grad(op, grad):
    # pylint: disable=unused-argument
    """
    straight through estimator logic used to compute gradient for Quantize Op.
    :param op: quantize op
    :param grad: gradient
    :return: gradients computed per input
    """

    dloss_by_dx = tf.cond(op.inputs[int(QuantizeOpIndices.is_int_data_type)], lambda: _compute_dloss_by_dx(op, grad),
                          lambda: grad)
    return dloss_by_dx, None, None, None, None, None, None, None


@tf.RegisterGradient("QcQuantizeRecurrentParam")
def _qc_recurrent_param_straight_through_estimator_grad(op, grad):
    # pylint: disable=unused-argument
    """
    straight through estimator logic used to compute gradient for Quantize Op.
    :param op: quantize op
    :param grad: gradient
    :return: gradients computed per input
    """
    dloss_by_dx = _compute_dloss_by_dx(op, grad)

    return dloss_by_dx, None, None, None, None, None, None, None


# pylint: disable=too-many-locals
def _compute_dloss_by_dmin_dmax_and_dx_symmetric(inputs: tf.Tensor,
                                                 bitwidth: tf.Tensor,
                                                 encoding_min: tf.Tensor,
                                                 encoding_max: tf.Tensor,
                                                 grad: tf.Tensor):
    """
    Calculate dloss_by_dmin, dloss_by_dmax, and dloss_by_dx tensors computed by symmetric quantization

    :param inputs: Inputs to op
    :param bitwidth: Bitwidth used to quantize
    :param encoding_min: Encoding min value(s), will be more than one if per channel is active
    :param encoding_max: Encoding max value(s), will be more than one if per channel is active
    :param grad: Gradient from child layer
    :return: Tensors for dloss_by_dmin, dloss_by_dmax, and dloss_by_dx
    """
    x = tf.cast(inputs, tf.float32)
    bitwidth = tf.cast(bitwidth, tf.float32)
    encoding_min = tf.cast(encoding_min, tf.float32)
    encoding_max = tf.cast(encoding_max, tf.float32)

    # handle min == max to avoid divide by zero
    epsilon = tf.constant(1e-5, dtype=tf.float32)
    encoding_max = tf.math.maximum(encoding_max, tf.add(encoding_min, epsilon))

    num_steps = tf.cast(tf.pow(tf.cast(tf.constant(2), tf.float32), bitwidth) - 1, tf.float32)
    half_num_steps = tf.divide(num_steps, tf.constant(2.0))
    delta = encoding_max / tf.math.floor(half_num_steps)
    offset = -tf.math.ceil(half_num_steps)

    zero = tf.zeros_like(num_steps)
    x_round = tf.round(inputs / delta) - offset
    x_quant = tf.clip_by_value(x_round, zero, num_steps)

    mask_tensor = tf.cast(tf.math.greater_equal(x_round, zero), tf.float32) * \
                  tf.cast(tf.math.less_equal(x_round, num_steps), tf.float32)
    grad_tensor = mask_tensor * grad

    axis = tf.cond(tf.equal(tf.rank(delta), 0),
                   lambda: tf.range(0, tf.rank(x)),         # Per-tensor
                   lambda: tf.range(0, tf.rank(x) - 1))     # Per-channel

    grad_encoding_max = tf.reduce_sum((x_quant + offset) * grad, axis=axis) - \
                        tf.reduce_sum(mask_tensor * (inputs / delta) * grad, axis=axis)

    grad_encoding_max = grad_encoding_max / tf.math.floor(half_num_steps)
    grad_encoding_max = tf.cast(grad_encoding_max, tf.float64)

    return tf.negative(grad_encoding_max), grad_encoding_max, grad_tensor


# pylint: disable=too-many-locals
def _compute_dloss_by_dmin_dmax_and_dx_asymmetric(inputs: tf.Tensor,
                                                  bitwidth: tf.Tensor,
                                                  encoding_min: tf.Tensor,
                                                  encoding_max: tf.Tensor,
                                                  grad: tf.Tensor):
    """
    Calculate dloss_by_dmin, dloss_by_dmax, and dloss_by_dx tensors computed by asymmetric quantization

    :param inputs: Inputs to op
    :param bitwidth: Bitwidth used to quantize
    :param encoding_min: Encoding min value(s), will be more than one if per channel is active
    :param encoding_max: Encoding max value(s), will be more than one if per channel is active
    :param grad: Gradient from child layer
    :return: Tensors for dloss_by_dmin, dloss_by_dmax, and dloss_by_dx
    """
    x = tf.cast(inputs, tf.float32)
    bitwidth = tf.cast(bitwidth, tf.float32)
    encoding_min = tf.cast(encoding_min, tf.float32)
    encoding_max = tf.cast(encoding_max, tf.float32)

    # handle min == max to avoid divide by zero
    epsilon = tf.constant(1e-5, dtype=tf.float32)
    encoding_max = tf.math.maximum(encoding_max, tf.add(encoding_min, epsilon))

    num_steps = tf.cast(tf.pow(tf.cast(tf.constant(2), tf.float32), bitwidth) - 1, tf.float32)
    delta = (encoding_max - encoding_min) / num_steps
    b_zero = tf.round(tf.negative(encoding_min) / delta)
    b_zero = tf.minimum(num_steps, tf.maximum(tf.constant(0.0), b_zero))
    offset = tf.negative(b_zero)

    zero = tf.zeros_like(num_steps)
    x_round = tf.round(inputs / delta) - offset
    x_quant = tf.clip_by_value(x_round, zero, num_steps)

    mask_tensor = tf.cast(tf.math.greater_equal(x_round, zero), tf.float32) * \
                  tf.cast(tf.math.less_equal(x_round, num_steps), tf.float32)
    grad_tensor = mask_tensor * grad

    grad_scale = (x_quant + offset - x * mask_tensor / delta) * grad
    grad_xq = delta * grad
    grad_offset = grad_xq * (1 - mask_tensor)

    axis = tf.cond(tf.equal(tf.rank(delta), 0),
                   lambda: tf.range(0, tf.rank(x)),         # Per-tensor
                   lambda: tf.range(0, tf.rank(x) - 1))     # Per-channel

    intermediate_term1 = tf.reduce_sum(grad_scale, axis=axis) / num_steps
    intermediate_term2 = num_steps / (encoding_max - encoding_min) ** 2 * tf.reduce_sum(grad_offset, axis=axis)

    grad_encoding_min = -intermediate_term1 + encoding_max * intermediate_term2
    grad_encoding_max = intermediate_term1 - encoding_min * intermediate_term2

    grad_encoding_max = tf.cast(grad_encoding_max, tf.float64)
    grad_encoding_min = tf.cast(grad_encoding_min, tf.float64)

    return grad_encoding_min, grad_encoding_max, grad_tensor



# pylint: disable=too-many-arguments
def _compute_dloss_by_dmin_dmax_and_dx_for_per_channel(inputs: tf.Tensor, bitwidth: tf.Tensor, op_mode: tf.Tensor,
                                                       encoding_min: tf.Tensor, encoding_max: tf.Tensor,
                                                       is_symmetric: tf.Tensor, is_int_data_type: tf.Tensor,
                                                       axis_handling: tf.Tensor, grad: tf.Tensor) -> \
                                                       Tuple[tf.Tensor, tf.Tensor, tf.Tensor]:
    """
    Return tensors for dloss_by_dmin, dloss_by_dmax, and dloss_by_dx in the case of per channel.
    :param inputs: Inputs to op
    :param bitwidth: Bitwidth used to quantize
    :param op_mode: Op mode (if passthrough, gradient is returned as is)
    :param encoding_min: Encoding min value(s), will be more than one if per channel is active
    :param encoding_max: Encoding max value(s), will be more than one if per channel is active
    :param is_symmetric: True if symmetric encodings are used, False otherwise
    :param is_int_data_type: True if op needs to operate with int data type, else False
    :param axis_handling: Determines behavior for reshaping inputs and gradients based on axis handling value.
    :param grad: Gradient from child layer
    :return: Tensors for dloss_by_dmin, dloss_by_dmax, and dloss_by_dx
    """
    @tf.function
    def reshape_input_and_grad_for_axis_handling(inputs, grad, axis_handling) -> Tuple[tf.Tensor, tf.Tensor]:
        """
        Reshape input and grad tensors from (H, W, channels, depth multiplier) to (H, W, channels * depth multiplier) in
        the case of axis_handling = LAST_TWO_AXES to get all channel elements in last dimension only.
        :param inputs: inputs to reshape
        :param grad: gradient to reshape
        :param axis_handling: Axis handling to determine reshape behavior
        :return: reshaped inputs and grad tensors
        """
        if tf.equal(axis_handling, tf.constant([AxisHandling.LAST_TWO_AXES.value])):
            # Even when in the case of inputs being a bias tensor, and axis handling will not be LAST_TWO_AXES, TF will
            # still execute both paths of the conditional branch to construct the graph. When doing so, if there are not
            # 4 dimensions to the tensor, the below code will fail, even though during session run we would not be going
            # down this path.
            # To fix this, add 3 dummy dimensions to the left side dimensions of the tensor such that we are guaranteed
            # to have at least 4 dimensions. Then continue with taking the rightmost 4 dimensions for the shape to
            # reshape to.
            inputs = tf.expand_dims(inputs, axis=0)
            inputs = tf.expand_dims(inputs, axis=0)
            inputs = tf.expand_dims(inputs, axis=0)
            orig_shape = tf.shape(inputs)
            inputs = tf.reshape(inputs, [orig_shape[-4], orig_shape[-3], orig_shape[-2] * orig_shape[-1]])
            grad = tf.reshape(grad, [orig_shape[-4], orig_shape[-3], orig_shape[-2] * orig_shape[-1]])
        return inputs, grad

    @tf.function
    def reshape_dloss_by_dx_for_axis_handling(inputs, dloss_by_dx, axis_handling) -> tf.Tensor:
        """
        Reshape dloss_by_dx tensor from (H, W, channels * depth multiplier) to (H, W, channels, depth multiplier) in
        the case of axis_handling = LAST_TWO_AXES to match shape with that of the weight tensor to update.
        :param inputs: inputs tensor to get original shape from
        :param dloss_by_dx: dloss_by_dx tensor to reshape
        :param axis_handling: Axis handling to determine reshape behavior
        :return: reshaped dloss_by_dx tensor
        """
        if tf.equal(axis_handling, tf.constant([AxisHandling.LAST_TWO_AXES.value])):
            # Even when in the case of inputs being a bias tensor, and axis handling will not be LAST_TWO_AXES, TF will
            # still execute both paths of the conditional branch to construct the graph. When doing so, if there are not
            # 4 dimensions to the tensor, the below code will fail, even though during session run we would not be going
            # down this path.
            # To fix this, add 3 dummy dimensions to the left side dimensions of the tensor such that we are guaranteed
            # to have at least 4 dimensions. Then continue with taking the rightmost 4 dimensions for the shape to
            # reshape to.
            inputs = tf.expand_dims(inputs, axis=0)
            inputs = tf.expand_dims(inputs, axis=0)
            inputs = tf.expand_dims(inputs, axis=0)
            orig_shape = tf.shape(inputs)
            dloss_by_dx = tf.reshape(dloss_by_dx, [orig_shape[-4], orig_shape[-3], orig_shape[-2], orig_shape[-1]])
        return dloss_by_dx

    reshaped_inputs, grad = reshape_input_and_grad_for_axis_handling(inputs, grad, axis_handling)
    dloss_by_dmin, dloss_by_dmax, dloss_by_dx = _calculate_gradients(reshaped_inputs,
                                                                     bitwidth,
                                                                     encoding_min,
                                                                     encoding_max,
                                                                     is_symmetric,
                                                                     op_mode,
                                                                     grad)

    dloss_by_dx = reshape_dloss_by_dx_for_axis_handling(inputs, dloss_by_dx, axis_handling)

    #return grad in case of floating-point mode
    dloss_by_dx = tf.cond(is_int_data_type, lambda: dloss_by_dx, lambda: grad)

    return dloss_by_dmin, dloss_by_dmax, dloss_by_dx


def _calculate_gradients(input_tensor: tf.Tensor,
                         bit_width: tf.Tensor,
                         encoding_min: tf.Tensor,
                         encoding_max: tf.Tensor,
                         use_symmetric_encoding: tf.Tensor,
                         op_mode: tf.Tensor,
                         grad: tf.Tensor) -> Tuple[tf.Tensor, tf.Tensor, tf.Tensor]:
    """
    Calculate dloss_by_dmin, dloss_by_dmax, and dloss_by_dx tensors

    :param input_tensor: Inputs tensor
    :param bit_width: Bitwidth used to quantize
    :param encoding_min: Encoding min value(s), will be more than one if per channel is active
    :param encoding_max: Encoding max value(s), will be more than one if per channel is active
    :param use_symmetric_encoding: Symmetric encoding boolean tensor
    :param op_mode: Op mode (if passthrough, gradient is returned as is)
    :param grad: Gradient from child layer
    :return: Tensors for dloss_by_dmin, dloss_by_dmax, and dloss_by_dx
    """

    def _asymmetric_gradients():
        return _compute_dloss_by_dmin_dmax_and_dx_asymmetric(input_tensor,
                                                             bit_width,
                                                             encoding_min,
                                                             encoding_max,
                                                             grad)

    def _symmetric_gradients():
        return _compute_dloss_by_dmin_dmax_and_dx_symmetric(input_tensor,
                                                            bit_width,
                                                            encoding_min,
                                                            encoding_max,
                                                            grad)

    dloss_by_dmin, dloss_by_dmax, dloss_by_dx = tf.cond(use_symmetric_encoding,
                                                        _symmetric_gradients,
                                                        _asymmetric_gradients)

    # Pass through gradient for skipped ops
    op_mode = tf.cast(op_mode, tf.int8)
    pass_through_mode = int(libpymo.TensorQuantizerOpMode.passThrough)
    dloss_by_dx = tf.cond(tf.equal(op_mode, pass_through_mode), lambda: grad, lambda: dloss_by_dx)
    dloss_by_dmin = tf.cond(tf.equal(op_mode, pass_through_mode),
                            lambda: tf.zeros_like(encoding_min, dtype=tf.float64),
                            lambda: dloss_by_dmin)
    dloss_by_dmax = tf.cond(tf.equal(op_mode, pass_through_mode),
                            lambda: tf.zeros_like(encoding_max, dtype=tf.float64),
                            lambda: dloss_by_dmax)

    return dloss_by_dmin, dloss_by_dmax, dloss_by_dx


@tf.RegisterGradient("QcQuantizeRangeLearningCustomGradient")
def quantsim_custom_grad_learned_grid(op, grad):
    """
    Performs custom gradient calculations for trained Quantize op

    :param op: Tf operation for which gradients are to be computed
    :param grad: Gradient flowing through
    """
    input_tensor = op.inputs[0]
    bit_width = op.inputs[int(QuantizeOpIndices.bit_width)]
    encoding_min = op.inputs[int(QuantizeOpIndices.encoding_min)]
    encoding_max = op.inputs[int(QuantizeOpIndices.encoding_max)]
    use_symmetric_encoding = op.inputs[int(QuantizeOpIndices.use_symmetric_encoding)]
    op_mode = op.inputs[int(QuantizeOpIndices.op_mode)]

    dloss_by_dmin, dloss_by_dmax, dloss_by_dx = _calculate_gradients(input_tensor,
                                                                     bit_width,
                                                                     encoding_min,
                                                                     encoding_max,
                                                                     use_symmetric_encoding,
                                                                     op_mode,
                                                                     grad)

    return dloss_by_dx, None, None, dloss_by_dmin, dloss_by_dmax, None, None, None


@tf.RegisterGradient("QcQuantizePerChannelRangeLearningCustomGradient")
def quantsim_per_channel_custom_grad_learned_grid(op, grad):
    """
    Performs custom gradient calculations for trained QcQuantizePerChannel op

    :param op: Tf operation for which gradients are to be computed
    :param grad: Gradient flowing through
    """
    dloss_by_dmin, dloss_by_dmax, dloss_by_dx = \
        _compute_dloss_by_dmin_dmax_and_dx_for_per_channel(op.inputs[0],
                                                           op.inputs[int(QuantizeOpIndices.bit_width)],
                                                           op.inputs[int(QuantizeOpIndices.op_mode)],
                                                           op.inputs[int(QuantizeOpIndices.encoding_min)],
                                                           op.inputs[int(QuantizeOpIndices.encoding_max)],
                                                           op.inputs[int(QuantizeOpIndices.use_symmetric_encoding)],
                                                           op.inputs[int(QuantizeOpIndices.is_int_data_type)],
                                                           op.inputs[int(QuantizeOpIndices.axis_handling)],
                                                           grad)
    return dloss_by_dx, None, None, dloss_by_dmin, dloss_by_dmax, None, None, None, None, None
