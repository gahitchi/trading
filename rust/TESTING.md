# Rust foundation — validation on Arch and Termux

Part 1 contains libraries only: no broker login, no trading CLI, no live orders.
Run every command from `rust/`, not the Python repository root.

## Arch Linux (primary validation)

From a terminal with `~/trading` already cloned:

```sh
sudo pacman -S --needed git base-devel rust
cd ~/trading
git fetch origin
git worktree add -b chatgpt/rust-engine-foundation-20261027 ../trading-rust origin/chatgpt/rust-engine-foundation-20261027
cd ../trading-rust/rust
rustc --version
cargo --version
cargo fmt --all -- --check
cargo test --workspace --all-targets
cargo clippy --workspace --all-targets -- -D warnings
cargo build --workspace --release
```

If the local branch or worktree already exists, use the existing `~/trading-rust/rust` directory; do not create a second worktree.

Capture validation evidence:

```sh
mkdir -p ../rust-test-results
{
  rustc --version
  cargo --version
  cargo fmt --all -- --check
  cargo test --workspace --all-targets
  cargo clippy --workspace --all-targets -- -D warnings
  cargo build --workspace --release
} 2>&1 | tee ../rust-test-results/arch-part1.log
```

For a pipeline that fails if any check fails, prefix it with `set -o pipefail`.
Rust >=1.85 is required for the edition 2024 workspace. The first compilation needs internet to fetch crates.
Commit the generated `Cargo.lock` after dependency review and successful tests.

## Android/Termux (portable correctness validation)

Install Termux from F-Droid/GitHub instead of using an obsolete Android store build.
Keep the repository under `$HOME`, **not** external/shared Android storage.

```sh
pkg update && pkg upgrade
pkg install git rust clang
git clone --depth 1 --single-branch --branch chatgpt/rust-engine-foundation-20261027 https://github.com/gahitchi/trading.git ~/trading-rust
cd ~/trading-rust/rust
rustc --version
cargo --version
cargo test --workspace --all-targets
cargo build --workspace
```

Optionally run `cargo fmt --all -- --check` and `cargo clippy --workspace --all-targets -- -D warnings` if those components are installed in your Termux Rust package.

If `rustc --version` is older than 1.85, upgrade Termux packages first.
Termux is native Android/Bionic, not Linux/glibc. Unit tests for fixed-point math,
book reconstruction and FSM should be portable, but Linux-specific thread affinity,
network interfaces, time-source behavior, perf benchmarking and production execution
will still need testing on Arch/Linux. Do not treat phone measurements as EC2/HFT latency.

## Interpretation

Passing unit tests demonstrates only the included invariants; the foundation has
no live ingestion, feed fixtures, order router, execution, accounting, strategy,
or broker integration. An untested branch cannot be called release-ready.
