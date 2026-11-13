# The shared Python venv (afic)

One Python virtual environment per machine serves every Arcade Foundry repo:
the app, every core and the tools. Nothing is installed into the system's
Python (Homebrew's and Debian's refuse it anyway), and no repo keeps a venv of
its own.

```sh
cd Arcade-Foundry-Insert-Coin-devel   # your app checkout, or any core checkout
git pull && git submodule update --init --recursive
cores/arcade-foundry-cores-common/tools/afic-venv/setup.sh   # in a core: common/tools/afic-venv/setup.sh
```

That makes `~/venvs/afic` (once) and installs [requirements.txt](requirements.txt),
the one list of every module the repos use. Run it again whenever that list
changes. `AFIC_VENV` puts the venv somewhere else; `PYTHON` picks the Python it
is made from (3.11 or later).

Who finds it:

- the cores' CMake (Lindbergh, Chihiro, ALLS, Model 1/2): when
  `Python3_EXECUTABLE` is not given, `$AFIC_VENV/bin/python3` (default
  `~/venvs/afic`) if it exists. A build folder configured before keeps the
  Python it found then: delete it or pass `-DPython3_EXECUTABLE=...`.
- the app's `scripts/build_cores.py`: the Python it runs under when that is a
  venv, otherwise the shared venv, otherwise its own Python.
- anything else: run it with `~/venvs/afic/bin/python`, or put
  `~/venvs/afic/bin` first on `PATH` (`source ~/venvs/afic/bin/activate`).

Folders that the old per-repo venvs left behind (`venv/` in a core checkout,
`cores/arcade-foundry-cores-lindbergh/venv` in the app, the ALLS
checkout's own `venv`) are no longer used and can be deleted.
