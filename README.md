Skybrush Server - AIMotionLab (SZTAKI) fork
===========================================
[Skybrush](https://skybrush.io/) is a droneshow software suite by CollMot Robotics, this repo is a fork of the server component, called [Skybrush Server](https://github.com/skybrush-io/skybrush-server). [Skybrush Live](https://skybrush.io/modules/live/) is the graphical client that connects to it. A name mentioned often in the codebase is Flockwave: it is the JSON protocol the client and server use to communicate, and every message they exchange, such as `UAV-TAKEOFF` or `OBJ-LIST`, is defined by a JSON schema in the `flockwave-spec` package. Flockwave is also what this software was called before it was renamed to Skybrush, which is why all the Python packages are called `flockwave.*`.

Relative to upstream, this fork contains:

- changes to the core Crazyflie functionality, and
- the **aimotionlab** extension, which works closely with the **libmotioncapture** extension that feeds motion capture data into the server.

What you need before starting
-----------------------------

- **Git**
- **Poetry** - see the [official installation instructions](https://python-poetry.org/docs/#installation). This guide assumes Poetry 2.x; check with `poetry --version`.
- **A Python interpreter between 3.9 and 3.11.** Python 3.11 is recommended. If you don't have a python 3.11 interpreter installed, you can use a conda environment, as described in a later section.

How Poetry works
----------------

Without Poetry, the usual way to set up a Python project is to create a virtual environment by hand, then install packages into it with `pip`, and manually keep track of what you installed. Poetry replaces that whole routine with a declarative description of what the project needs.

### You declare constraints, not versions

In `pyproject.toml` you list the packages the project depends on, together with *constraints* on their versions rather than one exact version:

```toml
click = "^8.1.7"      # at least 8.1.7, but below 9.0.0
trio = "0.24.0"       # exactly this version, nothing else
scipy = "^1"          # anything in the 1.x series
```
The exact syntax and ruleset is described in the [poetry docs](https://python-poetry.org/docs/). Packages have dependencies of their own, and those have dependencies too. Poetry takes every constraint in the project, including transitive ones, and searches for a combination of versions that satisfies all of them simultaneously. This is the main thing Poetry buys you over `pip`. The output of poetry's dependency resolution is recorded in `poetry.lock`: the exact version of *every* package involved, together with cryptographic hashes of the files to download, meaning that it describes every version of every package involved, rather than a "recipe" like `pyproject.toml` does. You can see the full list with `poetry show`.

### The lock file is committed, so everyone gets the same packages

`poetry.lock` is tracked in Git. When you clone this repository and run `poetry install`, Poetry does **not** re-solve anything - it reads the lock file and installs precisely the versions recorded there. Every developer, and every machine, ends up with an identical set of packages. That reproducibility is the reason the file exists, and the reason you should never edit it by hand.


The files involved
------------------

| File | What it is | Edited by |
| --- | --- | --- |
| `pyproject.toml` | Project metadata and dependency *constraints* | You (usually via `poetry add`) |
| `poetry.lock` | The exact resolved version of every package | Poetry only - never by hand |
| `poetry.toml` | Local configuration *for Poetry itself* | Rarely |
| `.venv/` | The virtual environment Poetry creates and manages | Poetry only |

`poetry.toml` and `pyproject.toml` have confusingly similar names but unrelated jobs: `pyproject.toml` describes *the project*, while `poetry.toml` holds settings for *the tool*. In this repository `poetry.toml` contains:

```toml
[virtualenvs]
create = true
in-project = true
```

`in-project = true` means the virtual environment is created as `.venv/` inside the repository directory, instead of being hidden away somewhere under your home directory. This is convenient - it keeps the environment next to the code, it is easy to delete and recreate, and editors such as VS Code find it automatically. Because this setting lives in a committed file, it applies to everyone who clones the repository; you do not need to configure anything yourself. `.venv/` is listed in `.gitignore` and is never committed.


Package source repos
----------------------------

Most Python packages are downloaded from [PyPI](https://pypi.org), the Python Package Index. Some of this project's packages are not on PyPI, so `pyproject.toml` declares additional *sources* - extra package indexes for Poetry to look in:

```toml
[[tool.poetry.source]]
name = "PyPI"
priority = "primary"

[[tool.poetry.source]]
name = "fury"
url = "https://pypi.fury.io/skybrush/"
priority = "supplemental"
```

The `priority` field tells Poetry how eagerly to use each index:

- **`primary`** - searched normally. Here that is PyPI, where the great majority of packages come from.
- **`supplemental`** - only consulted when a package cannot be found in a higher-priority source. This keeps Poetry from querying it needlessly.
- **`explicit`** - never searched at all. It is used *only* for packages that name it explicitly. Upstream uses this level for its private index.

Additionally, a dependency can pin itself to a particular index with a `source` field:

```toml
flockwave-spec = { version = "^1.80.0", source = "fury" }
```

`fury` is a [Gemfury](https://gemfury.com) package index hosted by the Skybrush developers at `https://pypi.fury.io/skybrush/`. Gemfury is a service for publishing Python packages outside of PyPI. This index is public - no credentials needed - and it supplies the `flockwave-*` packages that make up the bulk of the Skybrush server itself (`flockwave-app-framework`, `flockwave-conn`, `flockwave-ext`, `flockwave-gps`, `flockwave-spec` and friends), plus `pyledctrl`. If you watch the output of `poetry install` and notice packages arriving from an unfamiliar URL, this is why.

Which Python version
--------------------

**Prefer Python 3.11.**

Most Python packages are distributed as *wheels* - prebuilt, ready-to-unpack archives. Packages that contain compiled C or Fortran code must publish a separate wheel for each Python version, because compiled code is tied to the exact interpreter it was built against. When no matching wheel exists, `pip` falls back to downloading the source code and compiling it on your machine, which requires a full build toolchain and frequently fails for older packages on newer interpreters.

Two packages pinned in our lock file, `motioncapture 1.0a2` and `numpy 1.24.3`, publish no wheels above **CPython 3.11**. On Python 3.12 or newer, installation therefore falls back to compiling them from source and fails - with a wall of compiler errors that looks like a broken system rather than a version mismatch.

`pyproject.toml` encodes this limit:

```toml
python = ">=3.9,<3.12"
```

so Poetry refuses an unsuitable interpreter immediately, with a clear message, instead of letting you discover the problem several minutes into a failed build.

Python **3.9 and 3.10 should also work** - the constraint permits them, and the locked packages ship wheels for both. But 3.11 is what the project is developed and tested against in the lab, so it is the version to prefer if you have a choice.

**If your machine already has a suitable Python version, you are done here** - skip the next section entirely and go straight to [Installation](#installation). You can check with:

```bash
python3 --version
```


Getting Python 3.11 with Miniconda
----------------------------------

Recent Linux distributions often ship only a very new Python - Ubuntu 26.04, for instance, provides Python 3.14 - which this project cannot use. Installing an older Python system-wide is possible but invasive, and risks disturbing other software on the machine that depends on the system Python. Conda solves this cleanly.

- **conda** is a package and environment manager. It is not Python-specific; it can manage non-Python libraries as well. A conda environment does all that a venv can, and more. You can activate it, and install python packages, like with a venv, however, unlike with a venv, you can also specify a python version for it - this is what we're after.
- **Anaconda** is a large distribution that bundles conda together with hundreds of preinstalled scientific packages.
- **Miniconda** is a minimal installer containing just conda, Python, and a few essentials. You then install only what you actually need. This is what we want.

Install Miniconda by following the [official instructions](https://docs.anaconda.com/miniconda/), then create an environment containing Python 3.11:

```bash
conda create -n skybrush-py311 python=3.11
```

`skybrush-py311` is just a name - choose whatever you like, and use your choice consistently in the commands below. This installs Python 3.11 *inside the conda environment only*, under your Miniconda directory. Your system Python is untouched, and other projects on the machine are unaffected. You will need the path to that environment's interpreter in the next section. To find it:

```bash
conda env list
```

which prints each environment's location, for example `/home/<user>/miniconda3/envs/skybrush-py311`. The interpreter is at `bin/python` inside it. Note that you do **not** need to `conda activate` this environment to work on the project. We only use conda as a source of a Python 3.11 interpreter; Poetry takes over from there.


Installation
------------

### 1. Clone the repository

```bash
git clone <repository-url>
cd skybrush-server
```

### 2. Tell Poetry which interpreter to use

Skip this step if your system Python is already between 3.9 and 3.11 - Poetry will pick a suitable interpreter on its own. If you created a conda environment above, point Poetry at its interpreter:

```bash
poetry env use ~/miniconda3/envs/skybrush-py311/bin/python
```

This does *not* install anything into the conda environment. Poetry checks that the interpreter satisfies the project's Python constraint, then creates `.venv/` in the project directory using that interpreter as its base, and remembers it as this project's environment. The new environment starts empty - it does not inherit packages from conda.

Because `.venv/` links back to the conda environment, deleting or recreating that conda environment will break it. If that happens, delete `.venv/` and repeat this step.

### 3. Install the dependencies

```bash
poetry install
```

Poetry reads `poetry.lock` and installs the exact versions recorded there. Expect this to take a few minutes on a first run, as it downloads well over a hundred packages.

Besides the dependencies, this also installs **the flockwave-server package** into the environment, in editable mode. As a consequence:

- The commands `skybrushd`, `skybrush-gateway` and `skybrush-proxy` become available inside the environment. These are declared in the `[tool.poetry.scripts]` section of `pyproject.toml`. For example, `skybrushd = "flockwave.server.launcher:start"` maps the skybrushd command to the flockwave.server.launcher module's start function.
- Your edits to the code under `src/` take effect immediately. You do **not** need to reinstall after changing the source.


Working inside the environment
------------------------------

The packages are installed in `.venv/`, not system-wide, so the project's commands are not on your `PATH` by default. The simplest way to run something inside the environment is to prefix it with `poetry run`:

```bash
poetry run python
poetry run pytest
```

Alternatively you can activate the environment in your shell, so that everything you type afterwards uses it. Note that `poetry env activate` only *prints* the appropriate activation command rather than performing it, so wrap it in `eval`:

```bash
eval $(poetry env activate)
```

To leave the environment again, run `deactivate`.

Some commands are useful for inspecting what is installed:

```bash
poetry env info        # which interpreter and environment are in use
poetry show            # every installed package and its version
poetry show --tree     # the dependency tree, showing why a package is present
```


Adding or changing dependencies
-------------------------------

**Do not use `pip install` inside this environment.** It would install a package without recording it anywhere, so `pyproject.toml` and `poetry.lock` would no longer describe reality - and the next `poetry install` would silently undo your change. Anyone else cloning the repository would be missing the package entirely, with nothing to indicate why the project no longer runs. To add or remove a dependency, use:

```bash
poetry add requests
poetry remove requests
```

This performs the whole sequence in one step: it adds the package to `pyproject.toml` with a sensible constraint, re-solves the dependency graph, updates `poetry.lock`, and installs the package into `.venv/`. You can add the package with constraints as well:

```bash
poetry add "requests>=2.31"
```

**Commit both `pyproject.toml` and `poetry.lock` together.** They are two halves of one statement, and committing only the first leaves everyone else unable to reproduce your environment.

Two related commands, which are easy to confuse:

- `poetry lock` - re-reads `pyproject.toml` and brings `poetry.lock` back into agreement with it. By default it leaves already-locked packages at their current versions, so it is safe; use it after editing a constraint in `pyproject.toml` by hand.
- `poetry update` - deliberately upgrades packages to the newest versions the constraints allow, rewriting the lock file. This can change a great many versions at once. For a project like this one, which is pinned to a known-good set, run it only intentionally and test carefully afterwards.


A minimal Trio primer
---------------------

The server is built on [Trio](https://trio.readthedocs.io/), an async framework. You do not need to know it deeply to follow the code, but you do need the basics. Async functions can be declared like so: `async def something()`. These functions do not run when called - they return a coroutine. You can await that coroutine (the return value) to run the function: `await something()`. Awaiting is sequential: the coroutine runs inside the current task, which waits for it to finish. What `await` adds is that if the coroutine hits a checkpoint (`trio.sleep`, socket I/O, `Event.wait`), the scheduler may switch to another task while it waits. Concurrency, then, comes from having multiple tasks, which can be created with a nursery, not from `await` itself. Note that tasks share a thread, so they interleave: trio offers *concurrency*, not *parallelism*. For blocking or CPU-bound work, hand the work to `trio.to_thread.run_sync` or a subprocess. Ordinary synchronous code cannot `await`, however; `await` calls can only be placed in asynchronous calls. The restriction is one-way: `await` is only legal inside an `async def`, but async functions may call ordinary sync functions freely. Beware that a *blocking* sync call stalls the entire event loop, since it never reaches a checkpoint. So, we must "transition" from sync to async context: this is done with `trio.run(async_func)`, which starts the event loop, and runs `async_func` as the first task. The server does this exactly once, at [launcher.py:103](src/flockwave/server/launcher.py#L103).

**Nurseries own tasks.** A nursery is a scope that supervises child tasks:

```python
async with open_nursery() as nursery:
    nursery.start_soon(async_fn_1)   # returns immediately
    nursery.start_soon(async_fn_2)
# execution does not pass this line until BOTH children have finished
```

This is *structured concurrency*: tasks cannot outlive the block that started them. If a child crashes, the nursery cancels its siblings and propagates the exception. If the nursery is cancelled, every child is cancelled. A task can therefore never be silently orphaned - which matters a great deal when the tasks are talking to flying drones.

There are two ways to start a child. `start_soon(f)` (note that this is a synchronous call) schedules it and returns immediately. `await nursery.start(f)` (note that this is awaited) schedules it and *waits until the task signals that it is ready* by calling `task_status.started()`. The server uses the latter when startup order matters, such as when `DaemonApp` starts the extension manager.

**Cancel scopes** are how things get stopped. `CancelScope.cancel()` makes the next checkpoint inside that scope raise `Cancelled`, unwinding the task. Shutting the whole server down is just cancelling the main nursery's scope - that is all `AsyncApp.request_shutdown()` does.

**Memory channels** are Trio's queues: `open_memory_channel(n)` returns a send/receive pair, and `async for item in receiver` consumes them. The extension manager uses one as a work queue.

Why Trio at all: this server simultaneously talks to a Crazyradio dongle, a motion capture stream, several TCP sockets, and a websocket client - all of which spend nearly all their time waiting. Threads would need locks around shared drone state; async lets it stay single-threaded, and Trio's structured concurrency makes shutdown and error propagation predictable.


Step 1: the command
-------------------
The server is started using the following command: `poetry run skybrushd -c skybrushd.jsonc`. `skybrushd` is not a script in this repository - it is a stub generated in the virtual environment when the project was installed. Its entire contents:

```python
#!/home/…/skybrush-server/.venv/bin/python
import sys
from flockwave.server.launcher import start

if __name__ == '__main__':
    sys.exit(start())
```

So the real entry point is `start()` in [src/flockwave/server/launcher.py](src/flockwave/server/launcher.py). The mapping from command name to function is declared in `[tool.poetry.scripts]` in `pyproject.toml`:

```toml
[tool.poetry.scripts]
skybrushd = "flockwave.server.launcher:start"
```

`start()` is a [click](https://click.palletsprojects.com/) command. Command-line flags are declared as decorators above it, one per flag; this is the one that matters most to us:

```python
@click.option(
    "-c",
    "--config",
    type=click.Path(resolve_path=True),
    help="Name of the configuration file to load; defaults to "
    "skybrush.cfg in the current directory",
)
```

click turns each option into a function parameter. To add or change a command-line flag, add a `@click.option` decorator and a matching parameter to `start()`. `-c skybrushd.jsonc` on the command line arrives as the `config` argument:

```python
def start(
    config: str,
    port: Optional[int] = None,
    debug: bool = False,
    quiet: bool = False,
    log_style: str = "fancy",
):
```

The body then does six things, in order.

**1. Installs the log formatter**, before anything else has a chance to log:

```python
logger.install(
    level=logging.DEBUG if debug else logging.WARN if quiet else logging.INFO,
    style=log_style,
)
```

This is where `--debug`, `--quiet` and `--log-style` take effect. Immediately afterwards two loops raise the log level of noisy third-party libraries (`engineio`, `socketio`, `paramiko`, `httpx` and friends), which is worth knowing if you are ever missing log output you expected from a dependency.

**2. Loads `.env`**, so secrets and machine-specific settings can live outside the repo:

```python
dotenv.load_dotenv(verbose=debug)
```

**3. Applies a `--port` override**, by writing it into the environment rather than passing it along - the configuration layer reads `PORT` from the environment later:

```python
if port is not None:
    os.environ["PORT"] = str(port)
```

**4. Imports the application - deliberately late.** The comment in the source explains itself:

```python
# Note the lazy import; this is to ensure that the logging is set up by the
# time we start configuring the app.
from flockwave.server.app import app
```

Importing `flockwave.server.app` *constructs the application object as a side effect*, so the import has to happen after logging is configured. Moving this line to the top of the file, where imports normally go, would silently break log formatting during startup.

**5. Configures the application**, still synchronously - no event loop is running yet:

```python
retval = app.prepare(config, debug=debug)
if retval is not None:
    return retval
```

`prepare()` returns an error code if configuration failed, in which case the server exits without ever starting.

**6. Starts the server:**

```python
trio.run(app.run)
```

This is the transition from synchronous to asynchronous code. Everything past this line runs inside the Trio event loop. When it returns, the server has shut down, and the last statement in the function logs `"Shutdown finished"`.

### Running under a debugger

Because the server is started through a generated console script rather than a file in this repository, there is no obvious file to open in an editor and press "run" on. Fortunately the package provides a second, equivalent entry point. `src/flockwave/server/__main__.py` contains:

```python
import sys

if __name__ == "__main__":
    # Do not use relative imports here; it will confuse PyInstaller
    from flockwave.server.launcher import start

    sys.exit(start())
```

A `__main__.py` inside a package is what Python executes when you run that package with the `-m` switch, the same convention behind `python -m pip`. Its body is identical to the generated `skybrushd` stub shown above: both import `start` from the launcher and call it. So these two commands are equivalent, and the second one needs no console script:

```bash
poetry run skybrushd -c skybrushd.jsonc
poetry run python -m flockwave.server -c skybrushd.jsonc
```

Note that `__main__.py` itself is not interesting to debug - it is a door, not a room. Its value is that it gives a debugger something to launch, so that your breakpoints in `launcher.py`, `app.py` or an extension are reached with the right configuration file loaded.

This repository ships a VS Code debug configuration in `.vscode/launch.json` that does exactly that. Select `skybrushd` in the Run and Debug view (Ctrl+Shift+D) and press F5, and you get the equivalent of `poetry run skybrushd -c skybrushd.jsonc` with the debugger attached. A second configuration adds the server's own `--debug` flag, which raises the log level; despite the name it has nothing to do with the debugger.

Four of the settings in that file are load-bearing, and are worth understanding if you need to adapt it:

- `"module": "flockwave.server"` rather than `"program"` runs the package through `__main__.py`, exactly as the console script does, instead of pointing at a path inside the virtual environment.
- `"args": ["-c", "skybrushd.jsonc"]` supplies the configuration file. This is the reason a plain "run this file" button is not sufficient: with no `-c`, the server silently falls back to looking for `skybrush.cfg` and starts with an entirely different configuration.
- `"cwd": "${workspaceFolder}"` makes that relative path resolve against the repository root.
- `"justMyCode": false` allows breakpoints inside installed packages. Much of what the following sections describe - the extension manager, the application framework - lives in dependencies rather than in `src/`, and with the default setting the debugger silently skips breakpoints there.

Nothing about this approach is specific to VS Code; the requirements are only to run a module, pass arguments, set the working directory, and not skip library code. Without any editor at all, the standard library debugger can do the same, and `breakpoint()` placed anywhere in the source will drop a normally launched server into `pdb` when execution reaches it:

```bash
poetry run python -m pdb -m flockwave.server -c skybrushd.jsonc
```

One warning specific to this project. Trio is single-threaded, so a breakpoint stops *everything*, not just the task you are inspecting. While you sit at a breakpoint the server sends no commands and processes no motion capture frames, and connections may time out and be reopened by the supervisor. This is harmless when debugging startup or configuration, but think carefully before pausing the server with drones in the air.

Step 2: the application object
------------------------------

`src/flockwave/server/app.py` contains a module-level statement, which is therefore run when the module is imported:

```python
app = SkybrushServer("skybrush", PACKAGE_NAME)
```

This creates an instance of the `SkybrushServer` class named `app`. There is exactly one object of this type, created at import time - the import that the launcher deliberately delayed in the previous step. Its inheritance chain is: a `SkybrushServer` is a `DaemonApp`, which is an `AsyncApp`. The responsibilities are split across that chain:

| Class | Responsible for |
| --- | --- |
| `SkybrushServer` | Skybrush-specific things: registries, message handlers, UAV dispatch |
| `DaemonApp` | the extension manager, the connection supervisor, systemd integration |
| `AsyncApp` | configuration loading and the main nursery |

The two constructor arguments feed the lower two layers. `"skybrush"` is the app name, from which the framework derives the default configuration filenames (`skybrush.cfg`, `skybrush.jsonc`, `skybrush.json`) and the name of the environment variable that can point at another one, `SKYBRUSH_SETTINGS`. `PACKAGE_NAME` is not hardcoded; it is computed at the top of the same file:

```python
PACKAGE_NAME = __name__.rpartition(".")[0]
```

Inside `src/flockwave/server/app.py`, `__name__` is the string `"flockwave.server.app"`, so chopping off the last dotted component leaves `"flockwave.server"`. The app is thereby told which package it belongs to, and works out where to find both its default configuration and its extensions from that.

### Configuration loading

`AsyncApp` is responsible for configuration loading and the main nursery. Its `prepare()` method - called by the launcher before `trio.run()` - creates an `AppConfigurator` object.

The configurator starts from a configuration module: a normal `.py` file containing upper-case variable definitions. The default one is `flockwave.server.config`, which is the file `src/flockwave/server/config.py`.

The configurator then loads the configuration in three layers: the default config module, then the file passed with `-c` (e.g. `skybrushd.jsonc`), then an optional file named by the `SKYBRUSH_SETTINGS` environment variable, if it is set. Two rules govern how a layer combines with the ones before it, and `SkybrushServer` sets both:

```python
configurator.key_filter = str.isupper
configurator.merge_keys = ["EXTENSIONS"]
```

`key_filter` means that only upper-case top-level keys are treated as configuration. `merge_keys` names the keys that get special treatment, and `EXTENSIONS` is the only one: its value is merged recursively into the previous layer's instead of replacing it. Every other top-level key is overwritten outright by a later layer.

The recursive merge is what keeps `skybrushd.jsonc` short. The `crazyflie` extension is disabled in `config.py` and enabled in `skybrushd.jsonc`, so it ends up enabled - but only that one setting is replaced, and the rest of the crazyflie defaults (`id_format`, `fence`, `takeoff_altitude`) survive from `config.py`. Extensions that skybrushd.jsonc never mentions, such as `http_server`, `webui` and `show`, are still loaded with their defaults.

Once the layers are assembled, `prepare()` calls `_process_configuration()`, which `SkybrushServer` overrides. This is where a handful of settings are read out of the configuration dictionary and actually applied: the command execution timeout, and the base port - which may still be overridden by the `PORT` environment variable that the launcher set from `--port`. It also force-enables the `ext_manager` and `license` extensions regardless of what any config file said. Finally, `prepare()` returns either `None` or an error code, and as we saw, the launcher exits immediately on a code, so a bad configuration file stops the server before any async code runs.

### The main nursery

The main nursery is opened in `AsyncApp.run()` (reached from `trio.run(app.run)` by way of `SkybrushServer.run()`), and it is the parent of every task the server runs. Its lifetime *is* the server's lifetime: recall that an `async with open_nursery()` block does not exit until all its children have finished, so for as long as any task is still alive, `run()` is sitting inside that block. When the block finally exits, the server is done and `trio.run()` returns.

Having one nursery own everything gives the app a single place to put long-lived work, and `run_in_background(func)` is the method that puts it there. Any component or extension can hand it an async function, which then becomes a child task of the main nursery. Two options are worth knowing. A task can be marked *cancellable*, in which case it is given its own cancel scope and that scope is returned to the caller, who can then stop just that task without disturbing anything else. Or it can be marked *protected*, meaning an unexpected exception is logged and swallowed rather than propagating - remember that a crashing child normally cancels its siblings, so without this an extension's bug would take the whole server down with it.

`run_in_background(func)` either starts the task at once, or, if the nursery hasn't been set up yet, appends it to a `_pending_tasks` list, starting them in the order they were appended once the nursery is up.

`SkybrushServer.run()` immediately registers three tasks of its own (the message hub, the command execution manager and the rate limiters) before delegating upward:

```python
async def run(self) -> None:
    self.run_in_background(self.command_execution_manager.run)
    self.run_in_background(self.message_hub.run)
    self.run_in_background(self.rate_limiters.run)
    return await super().run()
```

Because the nursery is stored on the app, stopping the server is trivial: `request_shutdown()` calls `self._nursery.cancel_scope.cancel()`, which cancels every child task at once. Ctrl+C arrives at the same place, as `KeyboardInterrupt` is caught around the nursery block. Either way, the `finally` clause calls `teardown()` on the way out.

Step 3: DaemonApp and loading the extensions
--------------------------------------------

`DaemonApp` adds what a long-running server needs on top of a bare async app: an extension manager, a connection supervisor, and integration with systemd. `DaemonApp` overrides `_create_basic_components()`, which `AsyncApp.__init__()` calls:

```python
self.extension_manager = ExtensionManager(self._package_name + ".ext")
if ConnectionSupervisor: # if import was successful
    self.connection_supervisor = ConnectionSupervisor()
```

Both objects therefore exist from the moment `app.py` is imported - before any configuration file has been read, and long before the event loop starts.

## Connections

A *connection* in Skybrush is a stateful, reopenable link between the server process (which includes the extensions), and something outside it (such as a Crazyradio USB dongle, a serial port, or a service reachable over network like the OptiTrack host). `flockwave-conn` models them as `Connection` objects with four possible states: `DISCONNECTED`, `CONNECTING`, `CONNECTED`, `DISCONNECTING`. `Connections` can be opened, read from and written to, closed, and - crucially - reopened after failing.
To disambiguate from the everyday meaning of connection, such as a TCP connection: in the everyday sense, a TCP connection *is* the socket: when you disconnect and then reconnect, that is a new instance of a TCP connection (new file descriptor, new ephemeral port, no continuity).
A Skybrush `Connection` can wrap a succession of such TCP connections in a stable identity in the registry across drops, tracking state accordingly, emitting signals at state transitions.

Things that are *not* `Connections`:
- A Crazyflie drone: the `crazyflie` extension connects to a radio dongle. Individual Crazyflies are discovered *through* that single connection and modelled as UAV objects. One `Connection`, many drones.
- The Skybrush Live link: this is a connection in the everyday sense of the word: Live opens a websocket to an HTTP server that's maintained by the `http_server` extension.

Examples of actual `Connections`:
- The crazyradio dongle: The `crazyflie` extension opens one `Connection` per radio, declared in `skybrushd.jsonc`, tagged `uavRadioLink`.
- The motion capture driver process: the `libmotioncapture` extension spawns a child process (NOT a task in the same process) running its own `driver.py` which talks to OptiTrack and prints frames as JSON on its standard output. The connection is a `ProcessConnection` to that child's stdin/out, tagged `mocap`. So, the server's connection is to the **helper process**, and the helper holds the link to Motive. This is necessary because the underlying `libmotioncapture` library is blocking C++ code, which would stall the Trio event loop if called in the same process.

Because connections can fail, extensions do not usually open them directly. `DaemonApp` owns a connection supervisor, and an extension hands its connection over with `app.supervise(connection, task=...)`. The supervisor opens the connection, reopens it according to a retry policy whenever it drops, and runs the given task each time the link comes up, cancelling that task when it goes down.

Both of the connections above are supervised, which means extension code can be written as "while connected, do this" and ignore the transitions entirely - `libmotioncapture`'s frame loop is simply `async for frame in conn.iter_frames()`. If you add hardware of your own, use this mechanism rather than opening a socket by hand.

Most connections are written in the configuration file as URL-like strings and built by a factory, where the scheme selects the class to instantiate. `flockwave-conn` registers the general-purpose schemes - `serial`, `tcp`, `tcp-listen`, `udp`, `udp-listen`, `file`, `fd` and a few more - and an extension may register its own:

```python
create_connection.use(CrazyradioConnection, CrazyradioConnection.SCHEME)  # SCHEME == "crazyradio"
```

That single registration is what makes the radio entry in `skybrushd.jsonc` a legal one:

```jsonc
"connections": ["crazyradio://0/80/1M/E7E7E7E7"]
```

The point of the URL form is that the transport becomes a configuration decision rather than a code decision: the `rtk` extension, for instance, consumes corrections from a serial port or from a network source without any change to its own code.

Not every connection arrives this way, though. `libmotioncapture` is configured with a structured object rather than a string, and builds its `ProcessConnection` directly, turning the object's keys into command-line arguments for the child process:

```jsonc
"connections": [{ "hostname": "192.168.2.141", "type": "optitrack" }]
```

## Extensions

Most of what the Skybrush server actually does is implemented in extensions, such as the `aimotionlab` extension. The extension manager is handed `self._package_name + ".ext"`, which is plain string concatenation: the `"flockwave.server"` derived in the previous step becomes `"flockwave.server.ext"`. The manager keeps that string as a *package root*, which is what lets it turn an extension's name into a module, as described below.

### Where loading is triggered

`_on_nursery_created()` is a hook function that, once again, `DaemonApp` overrides. It is awaited in `AsyncApp.run()`, after opening the main nursery but before starting any queued task. `DaemonApp` uses it to start the extension manager:

```python
await nursery.start(partial(
    self.extension_manager.run,
    configuration=self.config.get("EXTENSIONS", {}),
    app=self,
))
```

The `EXTENSIONS` dictionary assembled previously is handed over. Then, the extension manager's `run` task is started, using `await nursery.start(...)` rather than `start_soon(...)`, so the server *blocks here until every enabled extension has finished loading*. If an extension raises an exception while loading, the exception is turned into an `ApplicationExit` and the server stops instead of half-starting.

### From a name to a module

In `skybrushd.jsonc` you write a key:

```jsonc
"aimotionlab": { "channel": 1, ... }
```

`"aimotionlab"` is an *extension name* - a plain dictionary key with no inherent connection to any file, and whether anything corresponds to it is not yet known. Resolving it is the job of the `ExtensionModuleFinder`, which the `ExtensionManager` creates at construction time and registers `"flockwave.server.ext"` on as its package root. The finder turns the extension name into a module name by prefixing it with that package root, giving `flockwave.server.ext.aimotionlab`. If no such module exists it also considers extensions registered by separately installed packages, and failing that reports `No such extension: aimotionlab` - the error you get after a typo in the configuration file.

From there, `import_module()` hands that name to the interpreter, which searches `sys.path` for it - and `src` is on `sys.path` because of the editable install - so it resolves to `src/flockwave/server/ext/aimotionlab/`, and the module is imported.

So the whole chain is:

```
"aimotionlab"                                  extension name (a key in skybrushd.jsonc)
    + "flockwave.server.ext"                   package root, from PACKAGE_NAME + ".ext"
  -> flockwave.server.ext.aimotionlab          module name
  -> src/flockwave/server/ext/aimotionlab/     file path, via sys.path
```

The practical consequence is that there is no registry to update: to create a new extension you add a directory or a single `.py` file under `src/flockwave/server/ext/`, and the name of that file or directory *is* the key you write in the configuration.

## Anatomy of an extension

An extension is extra code that extends the functionality of the Skybrush server. The interface that an extension presents to the server is defined by hook functions that contain the functionality the extension implements, and metadata attributes. In Python, modules are objects, and their attributes are essentially the names defined inside that module. Accordingly, for a simple extension that requires no state, a module with the correct hook functions and metadata variables defined can act as an extension itself. For more complex extensions with state, it is recommended to derive from the `Extension` class, and override the necessary functions and attributes.

When the manager loads an extension, it imports the module the extension's name resolved to - `myext.py` for a single-file extension, or `myext/__init__.py` for a directory - and then looks for a `construct` attribute:

```python
instance_factory = getattr(module, "construct", None)
extension = instance_factory() if instance_factory else module
```

If the `construct` attribute is defined, it will be called with no arguments, and its return will be *the* extension. `aimotionlab/__init__.py` takes this path in a single line:

```python
from .extension import aimotionlab as construct
```

Typically, the `construct` attribute will be the *class itself*, such that calling it produces an instance of the class, which is then presumed to contain the hook member functions. Note that this only changes where *hooks* are looked up: metadata attributes are always read from the module, so they belong in `__init__.py` even when the extension itself is a class.

If the module does *not* define `construct`, the module itself is the extension and any state lives in module-level variables. Several single-file extensions are written this way, including `signals.py`, `system_clock.py` and `tcp.py`.

Whichever shape you choose, the manager reads a handful of optional module-level attributes.
- `description`: one-line description, shown in the server's web UI
- `dependencies`: names of other extensions that must be loaded first
- `schema`: JSON Schema for this extension's configuration block.

### Configuration

The value under your extension's name in `skybrushd.jsonc` is passed as the second positional argument of the `load()`, `run()` and `worker()` hooks. It will be a dictionary with the same keys that `schema` describes.

```python
async def run(self, app: "SkybrushServer", configuration, logger):
```

Class-style extensions usually store it instead of passing it around. `ExtensionBase.load()` calls `configure()` once with the same object:

```python
def configure(self, configuration: Configuration) -> None:
    super().configure(configuration)
    self.configuration = configuration
```

Defaults belong in `src/flockwave/server/config.py`, since the layers described earlier merge the user's file into it recursively. That way an operator's configuration file only has to state what differs.

### Lifecycle hooks

All of these are optional; define only the ones you need. Their arguments are bound partially, so a hook may declare fewer parameters than are offered.

| Hook | Kind | When it runs |
| --- | --- | --- |
| `load(app, configuration, logger)` | sync | once, when the extension is loaded |
| `run(app, configuration, logger)` | **async** | spawned as a background task right after `load()`, for the lifetime of the server |
| `spinup()` | sync | when the first client connects |
| `worker(app, configuration, logger)` | **async** | spawned alongside `spinup()`, and cancelled on spindown |
| `spindown()` | sync | when the last client disconnects |
| `unload(app)` | sync | once, when the extension is unloaded |

`run` is where most extensions do their work. It is spawned as a task named `extension:<name>/run` and wrapped so that an unexpected exception is logged and contained, rather than cancelling the extension manager's nursery and taking the whole server down with it.
Should the extension's task be one that is per-client, rather than persistent from the server's start until shutdown, `worker` can be used instead of `run`. When the number of connected clients changes, the application tells the extension manager to spin up or down:

```python
def _on_client_count_changed(self, sender: ClientRegistry) -> None:
    if self.extension_manager:
        self.run_in_background(
            self.extension_manager.set_spinning, self.num_clients > 0
        )
```

So:

- `run()` executes from server start until shutdown, whether or not anyone is watching.
- `worker()` executes only while at least one client - in practice, Skybrush Live - is connected.


### Depending on other extensions

Declare dependencies as a module-level attribute, and the manager loads them first, recursively and with cycle detection:

```python
dependencies = ("crazyflie", "signals", "show")
```

That single line in `aimotionlab/__init__.py` guarantees those three are fully loaded before it starts.

### Extension API

An extension publishes an API by defining an `exports` dictionary, and consumes another's through `app.import_api()`. For example, `motion_capture` publishes two functions:

```python
exports = {"create_frame": create_frame, "enqueue_frame": enqueue_frame}
```

and `libmotioncapture` consumes them:

```python
create_frame = self.app.import_api("motion_capture").create_frame
enqueue_frame = self.app.import_api("motion_capture").enqueue_frame

conn.frame_factory = create_frame

async for frame in conn.iter_frames():
    enqueue_frame(frame)
```

which is why `libmotioncapture` declares `dependencies = ("motion_capture",)`: the API has to be there before the frames start arriving.

### Running background tasks

`run()` and `worker()` are started for you, but an extension often needs further tasks of its own. Rather than opening a bare nursery, use the helpers on `ExtensionBase`: `run_in_background()` for a single task, or the `use_nursery()` async context manager for a private nursery scoped to the extension. Both keep your tasks inside the structured-concurrency tree, so they are cancelled properly when the extension is unloaded or the server shuts down.

Step 4: SkybrushServer
----------------------

`SkybrushServer` is the otermost layer of the inheritance chain, and holds functionality specific to run drone (UAV) shows: registries for objects/drivers/connections/clients managed by the server, and message processing to objects such as crazyflie drones.

### The registries

`_create_components()`, declared and called in `AsyncApp`, overriden in `SkybrushServer` builds the server's data model, consisting of registries. Each registry is a lookip table for one kind of thing the server keeps track of. 

- `object_registry`: every object the server knows about, most importantly, UAVs 
- `uav_driver_registry`: the drivers that know how to talk to a given kind of UAV
- `connection_registry`: the `Connections` described above
- `client_registry`: the clients currently connected, e.g. Skybrush Live
- `channel_type_registry`: the transports a client may arrive on 
- `device_tree`: a node per UAV, and a subtree of devices and channels under each

Extensions usually add to these registries: the `crazyflie` extension is what puts Crazyflies into `object_registry` and its driver into `uav_driver_registry`; the `libmotioncapture` extension is what puts its `ProcessConnection` into `connection_registry`. The class attributes at the top of `SkybrushServer` in `app.py` document each one in more detail.

### The message hub

`message_hub: MessageHub` is the dispatch table for Flockwave messages: it maps each message type to a list of handler functions, and its `run()` task, started in `SkybrushServer.run()`, drains the queue of outgoing messages. The `on()` decorator appends a function to the list for the types named, which is why the bottom of `app.py` is a long series of them:

```python
@app.message_hub.on("CONN-INF")
def handle_CONN_INF(message: FlockwaveMessage, sender: Client, hub: MessageHub):
    return app.create_CONN_INF_message_for(message.get_ids(), in_response_to=message)
```

When a message of that type arrives, the hub calls every handler registered for (subscribed to) it. What a handler returns is a small protocol: `True` means it handled the message, `False` or `None` that it skipped it, and a returned response object is enqueued and sent back to the sender - which is why the handlers here end in `return app.create_..._message_for(...)` instead of sending anything themselves. An extension registers handlers the same way, which is how new message types are supported without touching the core. `register_message_handler()` returns a function that unregisters it again, for use when the extension is unloaded.

---

Featured extensions
--------------------

Four extensions matter most in this fork. Three of them carry motion capture data from OptiTrack to the drones, and form a chain: `libmotioncapture` is the vendor-specific source, `motion_capture` is the vendor-neutral middle layer, and `crazyflie` and `aimotionlab` are its two consumers. `crazyflie` is also what actually talks to the drones, so everything else ultimately goes through it.

### libmotioncapture

This extension's only job is to get frames out of a motion capture system and into the generic layer. It interprets nothing.

As described in the Connections section, it does not talk to OptiTrack directly: it spawns a child process running its own `driver.py`, which uses the `motioncapture` library and prints each frame to standard output as JSON. The `hostname` and `type` from its configuration block are passed to that child as command-line arguments, so switching to another mocap system is a configuration change rather than a code change.

The extension reads the child's standard output through the `ProcessConnection` described earlier, parsing each line of JSON into a frame. It imports two functions from `motion_capture` to do so: `create_frame` is installed as the connection's frame factory, so the frame objects are constructed by `motion_capture` rather than here, and `enqueue_frame` submits each finished frame to it.

```python
create_frame = self.app.import_api("motion_capture").create_frame
enqueue_frame = self.app.import_api("motion_capture").enqueue_frame

conn.frame_factory = create_frame

async for frame in conn.iter_frames():
    enqueue_frame(frame)
```

### motion_capture

This is the mocap-vendor-neutral layer, and the one everything downstream actually depends on. Nothing but `libmotioncapture` knows that the lab uses OptiTrack (as opposed to Vicon, for example). It does three things to every frame it receives.

**Rate limiting.** The `frame_rate` setting caps how often frames are passed on - as set in `skybrushd.jsonc`

**Name remapping.** The `mapping.rules` setting can rewrite the rigid body names coming from Motive. For example, stripping prefixes, so a body called `cf04` in Motive becomes `04`, which is the drone's id in the server.

**Fan-out.** Each surviving frame is emitted on the `motion_capture:frame` signal, using the `signals` extension. Anything interested subscribes to that signal, which is how one mocap source feeds several consumers. Two subscribe here: `crazyflie`, which sends each drone its own pose, and `aimotionlab`.

### crazyflie

This is the extension that actually flies the drones: it supplies the UAV driver for Crazyflies, the radio connections they are reached over, and the code that discovers them. Note that the code for this extension has been changed from the upstsream: `git log` on `src/flockwave/server/ext/crazyflie/` is the record of what the lab changed.

It is a class-style extension, `CrazyflieDronesExtension`, deriving from `UAVExtension` - the base class for extensions whose job is to provide a UAV driver. That base takes care of creating the driver and registering it in `uav_driver_registry`.

**Radios and discovery.** The extension registers the `crazyradio://` scheme, opens one connection per entry in its `connections` setting, and hands each to the connection supervisor together with a scanner task. The scanner continuously sweeps the radio's address space for drones that have been switched on; each one found is added to `object_registry` as a `CrazyflieUAV`, with its id formatted according to `id_format`. This is why drones may be powered up after the server has started and still appear.

**What the driver can do.** `CrazyflieDriver` implements the methods that `dispatch_to_uavs()` looks up - takeoff, landing, hovering, going to a position, arming, parameter get and set - and translates each into CRTP traffic. It also implements a set of `handle_command_*` methods, for example, sending the command `show` to a drone from Skybrush Live ends up in `handle_command_show()`. 

**Signals it subscribes to.** `motion_capture:frame`, to send each drone its own pose; `show:countdown` and `show:lights_updated`, so that trajectories, LED light programs and show timing reach the drones.

**What it exports.** A single function, `broadcast(port, channel, packet)`, which sends one CRTP packet to every drone on every radio at once rather than addressing them individually. This is what `aimotionlab` imports to push passive object poses out efficiently.

### aimotionlab

This is the lab's own extension, and the main reason this fork exists. It declares `dependencies = ("crazyflie", "signals", "show")` and has four jobs.

**Sending the poses of passive objects to the drones.** `crazyflie` already gives each drone its own pose; `aimotionlab` additionally forwards the poses of tracked objects that are *not* drones, for control purposes. Its frame handler picks out items whose names begin with `hook` or `test`, packs them into a CRTP external-pose packet, and sends it to the Crazyflies on the port and channel given by `cf_crtp_port` and `channel`, to let the drones know about payload/obstacle/hook pose.

**TCP ports for external scripts.** The aimotionlab extension also serves as a TCP server: the `tcp_ports` config block currently names four ports, each served by a different handler. These handlers are functions dispatched once for each client connection on that TCP port, typically long running functions that only exit when the client disconnects. These functions let scripts outside the server interact with the server, or the drones, or each other with the server as a broker (broadcast channel). 

| Name | Purpose |
| --- | --- |
| `drone` | a client connecting here gets a `DroneHandler` for issuing commands to one drone |
| `car` | broadcast channel, also notified when a show starts |
| `sim` | broadcast channel, also notified when a show starts |
| `lqr` | streams LQR parameters to a drone, and its trajectory timestamp back |

**The drone command protocol.** `DroneHandler` implements the commands a connected script may send over the `drone` port: `takeoff`, `land`, `hover`, `upload`, `start` and `set_param`. Uploading writes a trajectory into the Crazyflie's memory, which is where the `memory_partitions` configuration comes in: it carves that memory into numbered regions. Memory partitions marked as `dynamic` take part in on-the-fly trajectory upload: a trajectory being executed shouldn't be overwritten, therefore, we use two partitions to do this. One partition contains the trajectory being executed, the other can be freely written to. Note that server checks that a trajectory fits the partition it is given, but not whether the partitions themselves overlap - writing a sensible, non-overlapping config is the responsibility of whoever writes `skybrushd.jsonc`.

**Show integration.** The extension subscribes to three signals from the `show` extension. On `show:upload` it records the per-drone parameter changes uploaded alongside the trajectory - each is a show time, a parameter name and a value, which is how LQR gains are switched partway through a flight. On `show:clock_changed` it cancels and reschedules those pending changes against the new clock. On `show:start` it notifies the subscribed ports so that external scripts can start in step with the drones.

#### Using the DroneHandler class

The functionality offered by Skybrush Live is limited to interactions offered by the UI. Using [cflib](https://github.com/bitcraze/crazyflie-lib-python), it's possible to communicate with the drones from script, rather than UI, but that cuts the Skybrush server out of the picture. Using the aimotionlab extension's DroneHandler class, and the TCP server that establishes the drone handlers, one can reach the drones via the skybrush server. The TCP port's client side interface does not require flockwave at al, only a socket. This subsection describes the `drone` port from the client's side: its port can be checked in the `tcp_ports` section of `skybrushd.jsonc`.
`Dummy_Server.py` in the repository root serves this same protocol on the same ports, with no drones, no radio and no `flockwave` imports: it performs the handshake, accepts every command described below, replies exactly as the real server would, and prints what it would have done. Run it from the repository root, and stop the real server first - they bind the same ports.

To get a drone handler, open a TCP connection to the port and send `REQ_<drone id>` as UTF-8 text, as in "I'm requesting a handler to this drone". The server answers either with `ACK_<drone id>` if the drone is available (exists and has no handler), or `ACK_00` to signal that the request cannot be satisfied. Should a handler be established, commands can be dispatched to it on the tcp socket. Every command is a single byte string, which may include an argument if the command expects one:

```
CMDSTART_<command>[_<argument>]_EOF
```

These are the entries of `tcp_command_dict` ([drone_handler.py:305](src/flockwave/server/ext/aimotionlab/drone_handler.py#L305)):
- `takeoff`, with an argument specifying takeoff height
- `land`, no argument
- `hover`, no argument: stops the current trajectory being executed and initiates a long (300s) hover
- `upload`, with the trajectory as a raw JSON as argument: uploads the trajectory to the drone's memory, but does not start the traversal. The trajectory might be large enough to not fit in a single TCP fragent: the server will keep reading TCP fragments until it identifies the `EOF` matching the `CMDSTART`.
- `start`, with abs(olute) or rel(ative) as argument: starts traversing the last uploaded trajectory. The argument decides whether the trajectory is considered relative to the current position, or absolute in world coordinates
- `param`, the argument is a UTF-8 text shaped `name=value`, where `name` denotes which crazyflie [parameter](https://www.bitcraze.io/documentation/repository/crazyflie-firmware/master/userguides/logparam/), to set, and `value` denotes the value to set it to.

Successful commands return an ACK message, no response indicates that the handler crashed (meaning that ACKs should be awaited with a timeout).

**A minimal client.** Nothing more than this is required to command a drone:

```python
import trio

async def main():
    stream = await trio.open_tcp_stream("127.0.0.1", 6000)
    await stream.send_all(b"REQ_07")
    if await stream.receive_some() != b"ACK_07":
        raise RuntimeError("no handler for drone 07")
    await stream.send_all(b"CMDSTART_takeoff_0.5000_EOF")
    print(await stream.receive_some())   # b'ACK', or the socket closes

trio.run(main)
```

### Shows with Skybrush Live

A drone show is a set of predefined drone trajectories (and, optionally, lights) bundled together with metadata, defined by a skyc file (a .skyc extension file, which is *actually* a .zip file, meaning you can open it with winrar, 7zip, file roller, etc.). The default use case for our drones is flying them through a skyc defined drone show. 

#### What you need to set up before running a drone show:
1. Skybrush server installed.
2. [Skybrush Live](https://skybrush.io/modules/live/) installed.
3. [Crazyradio PA](https://www.bitcraze.io/products/crazyradio-pa/) or [Crazyradio 2.0](https://www.bitcraze.io/products/crazyradio-2-0/), to communicate with the drones using [CRTP protocol](https://www.bitcraze.io/documentation/repository/crazyflie-firmware/master/functional-areas/crtp/). Note that you'll need to [setup USB permissions on Linux](https://www.bitcraze.io/documentation/repository/crazyflie-lib-python/master/installation/usb_permissions/), or [install the USB driver on Windows](https://www.bitcraze.io/documentation/repository/crazyradio-firmware/master/building/usbwindows/). 
4. You'll need drones to fly. The lab uses [crazyflie drones](https://www.bitcraze.io/products/crazyflie-2-1-plus/), which need [skybrush compatible firmware](https://github.com/AIMotionLab-SZTAKI/crazyflie-firmware), and must be configured to match the channel and bitrate of the Crazyradio you're using. This can be done using  [cfclient](https://www.bitcraze.io/documentation/repository/crazyflie-clients-python/master/userguides/userguide_client/), which you might want to install anyway, since it offers helpful diagnostic options regarding crazyflie drones.
5. [OptiTrack camera system](https://www.optitrack.com/) set up.
6. [Motive (2.3.7) ](https://docs.optitrack.com/v2.3) up and running.
7. In Motive, you need to turn on tracking for the rigid bodies belonging to the drones you'll be flying. Note that these rigid bodies should be defined such that they are not symmetric along any axies, and they aren't mirror images of each other, or can be rotated into one another (else Motive might confue them).
8. The Skybrush server pc shall be **ethernet** connected to the network on which OptiTrack is streaming the motion capture data. 
9. You'll need the actual skyc file that defines the show. In the lab, the main way of creating one is using the [skyc_utils](https://github.com/AIMotionLab-SZTAKI/skyc_utils) package.

#### Launching a show
1. Launch Motive, and check whether all the rigid bodies belonging to the drones you'll fly in the show are tracked, and not flickering.
2. Launch Skybrush server with the skybrushd.jsonc config file: `poetry run skybrushd -c skybrushd.jsonc`. If there are any warnings, they should be addressed. Check the startup messages of the server: one of them should note that the libmotioncapture process was started, meaning the OptiTrack connection is working as intended:
```bash
[14:56:07]   server                 Starting Skybrush server 2.13.1
           ✔ skybrush               Loaded configuration from '/home/gaalbotond/SZTAKI/skybrush-server/skybrushd.jsonc'
[14:56:08]   logging                Storing logs in '/home/gaalbotond/.cache/Skybrush Server/log'
             logging                Logging started
             aimotionlab            The new extension is now running.
             http_server            Starting HTTP server on localhost:5000
             libmotionca            Using libmotioncapture connection
           ✔ libmotionca lmc/0      Started libmotioncapture process for 'Mocap connection 0 (optitrack)'
             crazyflie              Scanning Crazyflies from bradio://0/80/1M/E7E7E7E700 to ...3F
```
3. Launch Skybrush Live. Note the server's log saying a Client connected.
4. Place the drones near their takeoff positions defined in the (skyc) show file. During the show setup, they will be assigned trajectories based on which takeoff position they are closest do. You don't need to nail them exactly, but aim for a decent approximation of the actual takeoff positions.
5. Turn on the drones: if you did everything correctly, you should see them appear in Skybrush Live under the UAVs tab, indicating that the Crazyradio connection is functional. Check their Position and Heading: It should be stable, indicating that libmotioncapture connection to Motive is functional, and the cameras are calibrated properly.
6. Set up the show: select the file, and setup the takeoff area. This is when mapping the trajectories to the physical drones happens. When all is set up, you may upload the show data, and choose a start time: doing so will start a 15-second countdown. When the countdown expires, the show is started.



