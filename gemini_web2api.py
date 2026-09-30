#!/usr/bin/env python3
"""Backwards-compatible entry point for `python gemini_web2api.py`.

This file used to hold a complete second copy of the service (its own CONFIG,
its own payload builder, its own HTTP handler) alongside the `gemini_web2api/`
package. The two copies drifted: the single-file version knew nothing about
multiple accounts, and because each copy owned a *separate* CONFIG dict, the
image path in this copy delegated to the package's `multimodal.upload_image`,
which read the package's CONFIG -- so `--config` / `--cookie-file` silently did
not apply to image uploads (the request went out anonymous and without a proxy).

Everything now lives in the package, and this file is a thin shim so existing
scripts and documentation keep working:

    python gemini_web2api.py --config config.json      # still supported
    python -m gemini_web2api --config config.json      # preferred

Prefer the module form in new scripts and Docker images.
"""
import os
import sys


def main():
    # Import the package (the directory wins over this file's own module name)
    # and delegate. sys.path[0] is this script's directory, so the package is
    # importable exactly as it is when running `python -m gemini_web2api`.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from gemini_web2api.__main__ import main as package_main

    print("提示：python gemini_web2api.py 已改为兼容入口，"
          "等价于 python -m gemini_web2api（建议直接使用后者）", file=sys.stderr)
    return package_main()


if __name__ == "__main__":
    sys.exit(main() or 0)
