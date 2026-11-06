# Design documents

High-level designs for the parts shared by the cores: what each part is for,
how it is built and why, how it is tested and what is still open. They
describe the design as it is and are updated in the same pull request as a
change that moves it. The dated history is in HANDOFF.md and the current
state in STATE.md. The app's own designs are in Arcade Foundry: Insert
Coin's `docs/design/`.

They are public: they go out with each release and pass its scan, so they
name no people, machines or private paths and hold nothing taken from ROMs.
Working notes that shouldn't ship go in a `*-DEVELOPER.md` file.

| Document | Covers | State |
|---|---|---|
| [MTLGLES.md](MTLGLES.md) | mtlgles, OpenGL ES on Metal for the Lindbergh cores on Apple platforms | Mac done (opt-in); iPad comparison under way; tvOS 1.4.0 |
| [X86RECOMP.md](X86RECOMP.md) | The static x86 recompilers: 32-bit ELF and XBE (recomp.py), 64-bit PE (recomp64.py) | Lindbergh and Chihiro in use; 64-bit in development |

Each document follows the same outline as the app's: Purpose and scope,
Requirements, Design, Data and settings, Platforms, Testing, Open items,
Decisions.
