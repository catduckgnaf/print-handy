# Model intake resource limits

Print Handy streams supported 3MF mesh XML into compact float32 vertex and
int32 triangle buffers, retaining only the small model structure as XML.
Large geometry is not rejected at the former 32 MiB model XML threshold.
Unsupported XML layouts and encodings use the existing ElementTree fallback.

Safety limits in this revision:

| Resource | Limit |
| --- | --- |
| Download or local source | 200 MiB |
| ZIP members | 4,096 |
| Total expanded ZIP content | 512 MiB |
| Any expanded ZIP member | 400 MiB |
| Aggregate `.model` XML | 400 MiB |
| STL file | 32 MiB |
| XML triangles / vertices | 10,000,000 each |
| XML elements across models | 40,000,000 |
| Estimated geometry working storage | 1,536 MiB |

ZIP sizes are checked before opening any member. Model XML is then counted
with a streaming parser. XML document type declarations are refused. Binary
STL triangle counts are checked from the header when its length matches the
file. The STL byte limit also applies to ASCII STL. Site downloads count actual
streamed bytes even if Content-Length is missing or incorrect; failed partial
downloads are removed.

Discord upload, link and reprint intake check before title extraction, audit and
color prompts. Direct audit, color inventory and geometry loading apply the same
guard. A safety-ceiling refusal explains the exceeded budget and asks you to
simplify or split the file. Nothing is sliced or started for a refused intake.

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
