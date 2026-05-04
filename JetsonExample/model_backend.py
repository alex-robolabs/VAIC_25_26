from abc import ABC, abstractmethod
import os
import numpy as np

USE_CUDA = 0
USE_CORAL = 0

try:
    import pycuda.driver as cuda
    import common as cuda_common
    import tensorrt as trt
    USE_CUDA = 1
except ImportError:
    print("CUDA not found")

try:
    from pycoral.adapters import common as coral_common
    from pycoral.utils.edgetpu import make_interpreter, list_edge_tpus
    USE_CORAL = 1
except ImportError:
    print("Coral not found")

class ModelBackend(ABC):

    @property
    @abstractmethod
    def dtype(self):
        pass

    @abstractmethod
    def inference(self, image):
        pass

class CUDABackend(ModelBackend):

    @staticmethod
    def get_engine(onnx_file_path, engine_file_path=""):
        TRT_LOGGER = trt.Logger()
        # Attempts to load a pre-existing TensorRT engine, otherwise builds and returns a new one.

        def build_engine():
            print("Building engine file from onnx, this could take a while")
            # Builds and returns a TensorRT engine from an ONNX file.
            with trt.Builder(TRT_LOGGER) as builder, \
                    builder.create_network(cuda_common.EXPLICIT_BATCH) as network, \
                    builder.create_builder_config() as config, \
                    trt.OnnxParser(network, TRT_LOGGER) as parser, \
                    trt.Runtime(TRT_LOGGER) as runtime:

                config.set_memory_pool_limit(trt.MemoryPoolType.WORKSPACE, 1 << 28)  # 256 MiB workspace

                # Check if ONNX file exists
                if not os.path.exists(onnx_file_path):
                    print("ONNX file {} not found.".format(onnx_file_path))
                    exit(0)

                # Load and parse the ONNX file
                with open(onnx_file_path, "rb") as model:
                    if not parser.parse(model.read()):
                        print("ERROR: Failed to parse the ONNX file.")
                        for error in range(parser.num_errors):
                            print(parser.get_error(error))
                        return None

                # Set input shape for the network
                network.get_input(0).shape = [1, 320, 320, 3]

                # Build and serialize the network, then create and return the engine
                plan = builder.build_serialized_network(network, config)
                engine = runtime.deserialize_cuda_engine(plan)
                with open(engine_file_path, "wb") as f:
                    f.write(plan)
                return engine

        # Check if a serialized engine file exists and load it if so, otherwise build a new one
        if os.path.exists(engine_file_path):
            with open(engine_file_path, "rb") as f, trt.Runtime(TRT_LOGGER) as runtime:
                return runtime.deserialize_cuda_engine(f.read())
        else:
            return build_engine()

    def __init__(self):
        current_folder_path = os.path.dirname(os.path.abspath(__file__))
        onnx_file_path = os.path.join(current_folder_path, "models/pushback_lite.onnx")  # If you change the onnx file to your own model, adjust the file name here
        engine_file_path = os.path.join(current_folder_path, "models/pushback_lite.trt")  # This should match the .onnx file name

        # Get the TensorRT engine
        self.engine = CUDABackend.get_engine(onnx_file_path, engine_file_path)

        # Create an execution context
        self.context = self.engine.create_execution_context()

        # Allocate buffers for input and output
        self.inputs, self.outputs, self.bindings, self.stream = cuda_common.allocate_buffers(self.engine)

    def inference(self, image):
        self.inputs[0].host = image
        trt_outputs = cuda_common.do_inference_v2(self.context, bindings=self.bindings, inputs=self.inputs,
                                             outputs=self.outputs, stream=self.stream)
        
        return trt_outputs
    
    @property
    def dtype(self):
        return np.float32
    
class CoralBackend(ModelBackend):
    
    def __init__(self):
        current_folder_path = os.path.dirname(os.path.abspath(__file__))
        tflite_file_path = os.path.join(current_folder_path, "models/pushback_lite.tflite")

        devices = list_edge_tpus()
        if len(devices) == 0:
            print("No Coral device found. Please ensure it is plugged in")
            exit(-1)

        self.interpreter = make_interpreter(tflite_file_path)
      
        self.interpreter.allocate_tensors()

    def inference(self, image):
        coral_common.set_input(self.interpreter, image)
        self.interpreter.invoke()
        output_details = self.interpreter.get_output_details()
        outputs = [self.dequantize(details, coral_common.output_tensor(self.interpreter, i)) for i, details in enumerate(output_details)]

        return outputs

    def quantize(self, details, tensor):
        scale, zero_point = details["quantization"]
        if scale == 0:
            return tensor
        tensor = np.round(tensor / scale + zero_point).astype(np.int8)
        return tensor
    
    def dequantize(self, details, tensor):
        scale, zero_point = details["quantization"]
        if scale == 0:
            return tensor
        tensor = ((tensor.astype(np.float32) - zero_point) * scale)
        return tensor
    
    @property
    def dtype(self):
        return np.int8