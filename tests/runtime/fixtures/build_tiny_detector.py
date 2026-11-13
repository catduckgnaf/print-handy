"""Build the tiny synthetic failure-detector fixture for test_local_vision.py.

NOT the real model: a 3-op stand-in with the real model's I/O contract
(input ``input`` [1, 3, H, W] float; outputs ``boxes`` [1, N, 1, 4] and
``confs`` [1, N, 1]) whose confidences are a fixed function of the
frame's mean brightness, so tests can drive every verdict band with
solid-colour frames, fully offline:

* box 0 conf = sigmoid((mean - 0.35) * 30) — the "detector" confidence
* box 1 conf = box 0 conf * 0.9, heavily overlapping box 0 (NMS fodder)
* box 2 conf = relu(box 0 conf - 0.2), disjoint from box 0

Bright frame (mean ~0.9) -> conf ~1.0 -> spaghetti, 2 kept areas.
Mid frame (mean ~0.33) -> conf ~0.35 -> uncertain band.
Dark frame (mean ~0.05) -> conf ~0.0 -> ok.

Run with the repo venv (needs the ``onnx`` package, a build-time tool
only — it is not a runtime or test dependency):

    ~/workspace/projects/.venv-print-buddy/bin/python tests/runtime/fixtures/build_tiny_detector.py
"""
from pathlib import Path

import numpy as np
import onnx
from onnx import TensorProto, helper, numpy_helper

OUT = Path(__file__).with_name("tiny_failure_detector.onnx")

boxes = np.array(
    [[[[0.10, 0.10, 0.50, 0.50]]],
     [[[0.15, 0.15, 0.55, 0.55]]],
     [[[0.70, 0.70, 0.95, 0.95]]]], dtype=np.float32)  # [3, 1, 1, 4] pre-batched below
boxes = np.transpose(boxes, (2, 0, 1, 3))  # -> [1, 3, 1, 4]

inits = [
    numpy_helper.from_array(np.array([0.35], dtype=np.float32), "centre"),
    numpy_helper.from_array(np.array([30.0], dtype=np.float32), "gain"),
    numpy_helper.from_array(np.array([0.9], dtype=np.float32), "second_scale"),
    numpy_helper.from_array(np.array([0.2], dtype=np.float32), "third_drop"),
    numpy_helper.from_array(np.array([1, 3, 1], dtype=np.int64), "confs_shape"),
    numpy_helper.from_array(boxes, "boxes_const"),
]

nodes = [
    helper.make_node("ReduceMean", ["input"], ["mean"], keepdims=0, axes=[1, 2, 3]),
    helper.make_node("Sub", ["mean", "centre"], ["centred"]),
    helper.make_node("Mul", ["centred", "gain"], ["scaled"]),
    helper.make_node("Sigmoid", ["scaled"], ["conf0"]),
    helper.make_node("Mul", ["conf0", "second_scale"], ["conf1"]),
    helper.make_node("Sub", ["conf0", "third_drop"], ["third_raw"]),
    helper.make_node("Relu", ["third_raw"], ["conf2"]),
    helper.make_node("Concat", ["conf0", "conf1", "conf2"], ["confs_flat"], axis=0),
    helper.make_node("Reshape", ["confs_flat", "confs_shape"], ["confs"]),
    helper.make_node("Identity", ["boxes_const"], ["boxes"]),
]

graph = helper.make_graph(
    nodes,
    "tiny-failure-detector",
    inputs=[helper.make_tensor_value_info("input", TensorProto.FLOAT, [1, 3, 64, 64])],
    outputs=[
        helper.make_tensor_value_info("boxes", TensorProto.FLOAT, [1, 3, 1, 4]),
        helper.make_tensor_value_info("confs", TensorProto.FLOAT, [1, 3, 1]),
    ],
    initializer=inits,
)
model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
model.ir_version = 8
onnx.checker.check_model(model)
onnx.save(model, OUT)
print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")
