# Version 0.1.0

Local receipt OCR, source-linked review, duplicate detection, and exact CSV, JSON, and Excel exports..

Local verification: 16 tests passed; 90% measured coverage; Ruff lint and formatting checks passed. README quick start was exercised with fictional data.

## Known limitations

One PDF is treated as one invoice; common-label extraction is heuristic. Accuracy evidence covers generated fixtures only. HEIC and Tesseract are optional. POSIX input safety requires Linux or macOS. In-process RapidOCR has no hard wall-clock timeout.

See README and AGENTS.md for installation, configuration, machine-readable CLI/MCP interfaces and operational boundaries.
