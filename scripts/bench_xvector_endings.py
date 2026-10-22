#!/usr/bin/env python3
"""Reuse the isolated layout runner for six new natural-ending text pairs."""

import bench_generation_layout as layout

CASES = [
    "今天就先聊到这里吧。",
    "别着急，我还在这里陪着你呢。",
    "刚才说的那件事，你都记住了吗？",
    "放心，这件事我已经处理好了。",
    "桌上有个小盒子，旁边放着一把椅子。",
    "谢谢你一直听我说话，我现在好多了。",
]


if __name__ == "__main__":
    # Keep its checked GPU ownership, seed schedule and default sampling intact.
    layout.TEXTS = CASES
    layout.HOLDOUT = []
    layout.PROFILES = {"baseline": {}, "xvector": {"xvec_only": True}}
    layout.main()
