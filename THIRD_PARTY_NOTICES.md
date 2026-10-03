# Protocol implementation references

`antigravity.py` independently implements the Cloud Code protocol, informed by
[omp-antigravity-pro](https://github.com/ART1KZ/omp-antigravity-pro) and
[oh-my-pi](https://github.com/can1357/oh-my-pi). The routing, OAuth application
configuration and consumer-project fallback were ported from those sources.
No OMP runtime dependency is introduced.

MIT License

Copyright (c) 2026 ART1KZ
Copyright (c) 2025 Mario Zechner
Copyright (c) 2025-2026 Can Bölük
Copyright (c) 2026 Stencil Labs, Inc.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

Codex OAuth protocol reference: [openai/codex](https://github.com/openai/codex),
`codex-rs/login/src/{server,device_code_auth}.rs`. `native_auth.py` is an
independent Python implementation; no Rust source is included.

Codex quota protocol reference: `codex-rs/backend-client/src/client/rate_limit_resets.rs`
and the backend OpenAPI rate-limit status/window models. `codex_pool.py` is an
independent stdlib Python implementation of that protocol.

## Unreal Agent library used by the live adapter

`live-runner/` links the public library `github.com/unreallabsai/unreal-agent`
at v0.2.0. The CLI default system prompt and provider/tool configuration follow
its runner contract. Coordinator, inbox, context, durable operations and session
storage are provided by the upstream library, not copied/forked in this client.

MIT License

Copyright (c) 2026 Unreal Labs

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
