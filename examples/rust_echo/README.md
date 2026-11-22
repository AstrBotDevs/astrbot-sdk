# Rust Echo Example Plugin

Standalone native Rust plugin speaking AstrBot protocol-v1 over stdio.

## Build & Run

```sh
cargo build --example rust_echo -p astrbot-worker --release
mkdir -p examples/rust_echo/bin
cp target/release/examples/rust_echo examples/rust_echo/bin/rust_echo
cargo run -p astrbot-wire --bin astrbot-wire-conformance -- --id astrbot.rust_echo -- examples/rust_echo/bin/rust_echo examples/rust_echo
```
