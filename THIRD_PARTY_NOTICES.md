# Third-party notices

`kb_platform/vendor/pageindex_md.py` contains three Markdown indexing functions
from [VectifyAI/PageIndex](https://github.com/VectifyAI/PageIndex), commit
`b81465d9ad3d80ee1df344508a85d1f2cdfb8a26`.

Copyright (c) 2025 Vectify AI. These functions are distributed under the MIT
License. The original license is retained in
[`kb_platform/vendor/pageindex-LICENSE`](kb_platform/vendor/pageindex-LICENSE).

Python dependencies and external services retain their respective licenses.
Dependency versions are recorded in `uv.lock`. Model weights, private datasets,
provider credentials, and the full external PageIndex SDK are not bundled.

The optional formal-validation prerequisite is the official MIT-licensed
[TLA+ tools v1.7.4](https://github.com/tlaplus/tlaplus/releases/tag/v1.7.4).
`formal/toolchain-v1.json` records its source commit, download URL and integrity
hashes. The jar is not redistributed in this repository; explicit installation
verifies the locked bytes. TLA+ source licensing is available in the
[upstream license](https://github.com/tlaplus/tlaplus/blob/5a47802b5c391f59ecdd44117981f4ff8c0656ba/LICENSE).
