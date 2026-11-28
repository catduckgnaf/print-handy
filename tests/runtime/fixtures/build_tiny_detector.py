"""Build the tiny synthetic failure-detector fixture for test_local_vision.py.

NOT the real model: a 4-op stand-in with PrintGuard Standard's ONNX I/O
contract (input ``input`` [1, 3, 64, 64] float32 ImageNet-normalized NCHW;
output ``prob`` [1] float32 failure probability in (0, 1)) whose score is
a fixed function of the input tensor's mean brightness, so tests can drive
every verdict band with solid-color frames, fully offline:

* prob = sigmoid((ReduceMean(input) - (-0.85)) * 1.5)

With ImageNet normalization on solid RGB (v, v, v) frames:
* Bright frame (v=235, mean ~+2.09) -> prob ~0.99 (>= 0.75) -> spaghetti.
* Mid frame (v=84, mean ~-0.53) -> prob ~0.62 (0.50..0.75) -> uncertain band.
* Dark frame (v=13, mean ~-1.76) -> prob ~0.20 (< 0.50) -> ok.

Standard library only (writes ONNX IR v8 / opset 13 protobuf directly):

    python3 tests/runtime/fixtures/build_tiny_detector.py
"""
from __future__ import annotations

import struct
from pathlib import Path

OUT = Path(__file__).with_name("tiny_failure_detector.onnx")

# Protobuf wire types
_VARINT = 0
_LEN = 2

# ONNX TensorProto.DataType
_FLOAT = 1

# ONNX AttributeProto.AttributeType
_INT = 2
_INTS = 7


def _varint(value: int) -> bytes:
    if value < 0:
        value &= (1 << 64) - 1
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _field_varint(field: int, value: int) -> bytes:
    return _varint((field << 3) | _VARINT) + _varint(value)


def _field_bytes(field: int, payload: bytes) -> bytes:
    return _varint((field << 3) | _LEN) + _varint(len(payload)) + payload


def _field_str(field: int, text: str) -> bytes:
    return _field_bytes(field, text.encode("utf-8"))


def _attr_int(name: str, value: int) -> bytes:
    return _field_str(1, name) + _field_varint(3, value) + _field_varint(20, _INT)


def _attr_ints(name: str, values: list[int]) -> bytes:
    body = _field_str(1, name)
    for v in values:
        body += _field_varint(7, v)
    return body + _field_varint(20, _INTS)


def _node(op_type: str, inputs: list[str], outputs: list[str], attrs: list[bytes] = ()) -> bytes:
    parts = [_field_str(1, i) for i in inputs]
    parts += [_field_str(2, o) for o in outputs]
    parts.append(_field_str(4, op_type))
    parts += [_field_bytes(5, a) for a in attrs]
    return b"".join(parts)


def _float_initializer(name: str, values: list[float], dims: list[int]) -> bytes:
    parts = [_field_varint(1, d) for d in dims]
    parts.append(_field_varint(2, _FLOAT))
    parts.append(_field_str(8, name))
    raw = struct.pack(f"<{len(values)}f", *values)
    parts.append(_field_bytes(9, raw))
    return b"".join(parts)


def _value_info(name: str, dims: list[int]) -> bytes:
    shape = b"".join(_field_bytes(1, _field_varint(1, d)) for d in dims)
    tensor_type = _field_varint(1, _FLOAT) + _field_bytes(2, shape)
    type_proto = _field_bytes(1, tensor_type)
    return _field_str(1, name) + _field_bytes(2, type_proto)


def build_model() -> bytes:
    nodes = [
        _node("ReduceMean", ["input"], ["mean"], [
            _attr_ints("axes", [1, 2, 3]),
            _attr_int("keepdims", 0),
        ]),
        _node("Sub", ["mean", "center"], ["centered"]),
        _node("Mul", ["centered", "gain"], ["scaled"]),
        _node("Sigmoid", ["scaled"], ["prob"]),
    ]
    inits = [
        _float_initializer("center", [-0.85], [1]),
        _float_initializer("gain", [1.5], [1]),
    ]
    graph = b"".join([
        *(_field_bytes(1, n) for n in nodes),
        _field_str(2, "tiny-failure-detector"),
        *(_field_bytes(5, init) for init in inits),
        _field_bytes(11, _value_info("input", [1, 3, 64, 64])),
        _field_bytes(12, _value_info("prob", [1])),
    ])
    opset = _field_str(1, "") + _field_varint(2, 13)
    return b"".join([
        _field_varint(1, 8),  # ir_version = 8
        _field_bytes(7, graph),
        _field_bytes(8, opset),
    ])


def main() -> None:
    payload = build_model()
    OUT.write_bytes(payload)
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
