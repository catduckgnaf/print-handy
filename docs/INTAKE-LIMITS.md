# Model intake resource limits

Print Handy streams supported 3MF mesh XML into compact float32 vertex and
int32 triangle buffers, retaining only the small model structure as XML.
Large geometry is not rejected at the former 32 MiB model XML threshold.
Unsupported XML layouts and encodings use the existing ElementTree fallback.

Safety limits in this revision:

| Resource | Limit |
| --- | --- |
| Download or local source | No byte cap; refused only when the volume would drop below 512 MiB free |
| ZIP members | 4,096 |
| Standalone 3MF expanded ZIP total, members, and `.model` XML | No fixed byte cap; stream-counted geometry budget applies |
| Standalone 3MF ZIP compression ratio | At most 100:1 for members above 1 MiB expanded |
| STL file | No byte cap; triangle count (binary header, ASCII facets) under the triangle budget |
| XML triangles / vertices | 10,000,000 each |
| XML elements across models | 40,000,000 |
| Estimated geometry working storage | 1,536 MiB |

ZIP member counts and compression ratios are checked before opening any member.
The 400 MiB model XML, 400 MiB member and 512 MiB expanded limits no longer
apply to standalone 3MF sources, including model-site links. Model XML is
counted with a streaming parser; geometry and XML-element budgets still bound
the parser's work. XML document type declarations are refused. Binary
STL triangle counts are checked from the header when its length matches the
file. ASCII STL is counted by its facets with a streaming read, and a file that
is neither is sized as a binary STL would be, so no STL skips the triangle
budget. There is no download or STL byte cap (#1824): a download, upload or
archive extraction is stopped when it would leave less than 512 MiB free on
the volume, and the partial file is removed. A standalone 3MF ZIP member
expanded above 1 MiB and compressed more than 100:1 is refused as a zip bomb.
A separately uploaded `.zip` bundle follows its own extraction limits.

Discord upload, link and reprint intake check before title extraction, audit and
color prompts. Direct audit, color inventory and geometry loading apply the same
guard. A geometry refusal names the file and the measured triangle or vertex
count (at least the count seen when the limit was crossed), including for
model-site links. It does not assume the source can be edited in a modeling app.
Nothing is sliced or started for a refused intake.

## Large-source heads-up

A source under the limits above can still take minutes to check and combine.
When the downloaded files hold 20 MB of mesh or more, or 400,000 triangles
or more (a 3MF is measured by its unpacked mesh, a binary STL by its header
count), the intake posts one line right after the download, naming the size
and that preparing it takes a few minutes. Smaller sources post nothing extra.

## Verification limits

Geometry layout, transforms, topology, mesh audit and part annotation now
consume the same indexed vertex/face arrays instead of re-expanding the mesh
into Python objects, and color inventory scans members without building the
model DOM. Meshes the streaming reader declines still take the ElementTree
fallback, whose memory use this work does not bound. Synthetic
streaming-success and allocation-bound tests cover the parser and individual
consumers; they do not by themselves establish an end-to-end RSS guarantee.
Private-model capped intake and successful slicing acceptance remains
required before claiming this revision solves the observed memory problem.
OrcaSlicer's memory use must be measured separately from Print Handy's.

[Documentation home](README.md)
