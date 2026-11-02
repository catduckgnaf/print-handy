"""Print the digests that identify a pushed image: its config digest, then its image-manifest digest.

Used by docker-publish.yml and nightly.yml to prove the pushed image is the one acceptance ran against:
``docker image inspect .Id`` is the config digest on Docker's classic image store and the manifest
digest on the containerd store, so the caller accepts either line (2.2.5 review).

    python3 scripts/pushed_image_ids.py IMAGE DIGEST OS/ARCH
"""
import json
import subprocess
import sys


def manifest(ref):
    out = subprocess.run(["docker", "buildx", "imagetools", "inspect", "--raw", ref],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def main(image, digest, platform):
    doc = manifest(f"{image}@{digest}")
    manifest_digest = digest
    if "manifests" in doc:
        os_name, arch = platform.split("/")
        entry = next(m for m in doc["manifests"]
                     if m.get("platform", {}).get("os") == os_name
                     and m.get("platform", {}).get("architecture") == arch
                     and m.get("annotations", {}).get("vnd.docker.reference.type") != "attestation-manifest")
        manifest_digest = entry["digest"]
        doc = manifest(f"{image}@{manifest_digest}")
    print(doc["config"]["digest"])
    print(manifest_digest)


if __name__ == "__main__":
    main(*sys.argv[1:4])
