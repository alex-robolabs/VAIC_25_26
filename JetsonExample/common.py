#
# Copyright (c) 2021, NVIDIA CORPORATION. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import argparse
import os

import numpy as np
import pycuda.autoinit
import pycuda.driver as cuda
import tensorrt as trt

try:
    # Sometimes python does not understand FileNotFoundError
    FileNotFoundError
except NameError:
    FileNotFoundError = IOError

EXPLICIT_BATCH = 1 << (int)(trt.NetworkDefinitionCreationFlag.EXPLICIT_BATCH)

def GiB(val):
    return val * 1 << 30


def add_help(description):
    parser = argparse.ArgumentParser(description=description, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    args, _ = parser.parse_known_args()


# Simple helper data class that's a little nicer to use than a 2-tuple.
class HostDeviceMem(object):
    def __init__(self, host_mem, device_mem):
        self.host = host_mem
        self.device = device_mem

    def __str__(self):
        return "Host:\n" + str(self.host) + "\nDevice:\n" + str(self.device)

    def __repr__(self):
        return self.__str__()

# Allocates all buffers required for an engine, i.e. host/device inputs/outputs.
# TRT 10 uses named-tensor I/O: iterate via num_io_tensors + get_tensor_name,
# replacing the deprecated positional-binding APIs (get_binding_shape, max_batch_size,
# binding_is_input). With explicit batch the batch dim is part of the tensor shape,
# so size = trt.volume(shape) without the old max_batch_size multiplier.
def allocate_buffers(engine):
    inputs = []
    outputs = []
    bindings = []
    stream = cuda.Stream()
    for i in range(engine.num_io_tensors):
        name = engine.get_tensor_name(i)
        shape = engine.get_tensor_shape(name)
        size = trt.volume(shape)
        dtype = trt.nptype(engine.get_tensor_dtype(name))
        # Allocate host and device buffers
        host_mem = cuda.pagelocked_empty(size, dtype)
        device_mem = cuda.mem_alloc(host_mem.nbytes)
        # Append the device buffer to device bindings.
        bindings.append(int(device_mem))
        # Append to the appropriate list.
        if engine.get_tensor_mode(name) == trt.TensorIOMode.INPUT:
            inputs.append(HostDeviceMem(host_mem, device_mem))
        else:
            outputs.append(HostDeviceMem(host_mem, device_mem))
    return inputs, outputs, bindings, stream

# Internal helper: TRT 10 inference using execute_async_v3 with named tensor addresses.
# execute_async_v2 (which took bindings as a positional list) is removed in TRT 10;
# the replacement requires set_tensor_address(name, ptr) for each I/O before execute_async_v3.
def _do_inference_trt10(context, bindings, inputs, outputs, stream):
    # Transfer input data to the GPU.
    [cuda.memcpy_htod_async(inp.device, inp.host, stream) for inp in inputs]
    # Set tensor addresses for execute_async_v3 (named-tensor I/O).
    engine = context.engine
    for i in range(engine.num_io_tensors):
        context.set_tensor_address(engine.get_tensor_name(i), bindings[i])
    # Run inference.
    context.execute_async_v3(stream_handle=stream.handle)
    # Transfer predictions back from the GPU.
    [cuda.memcpy_dtoh_async(out.host, out.device, stream) for out in outputs]
    # Synchronize the stream
    stream.synchronize()
    # Return only the host outputs.
    return [out.host for out in outputs]

# This function is generalized for multiple inputs/outputs.
# inputs and outputs are expected to be lists of HostDeviceMem objects.
# In TRT 10 batch is always explicit (encoded in the tensor shape), so batch_size is ignored.
def do_inference(context, bindings, inputs, outputs, stream, batch_size=1):
    return _do_inference_trt10(context, bindings, inputs, outputs, stream)

# This function is generalized for multiple inputs/outputs for full dimension networks.
# inputs and outputs are expected to be lists of HostDeviceMem objects.
def do_inference_v2(context, bindings, inputs, outputs, stream):
    return _do_inference_trt10(context, bindings, inputs, outputs, stream)
