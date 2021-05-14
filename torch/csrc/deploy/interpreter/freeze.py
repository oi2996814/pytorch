"""
Freeze Python packages.

Freezing makes it possible to ship arbitrary Python modules as part of a C++
library. The Python source of the module is compiled to bytecode and written
to `.c` files, to be imported by Python's built-in FrozenImporter.

In a normal Python installation, FrozenImporter is only used to bootstrap the
initialization of the import machinery. Python's importers are defined in
Python (see `_bootstrap.py` and `_bootstrap_external.py`) but need to be
retrieved before any importers are available. Freezing the module bytecode
resolves this circular dependency.

This script will freeze the Python standard library. It produces two things:
- Bytecode files: A set of `.c` that define C variables containing Python bytecode.
- Main file: A `main.c` file listing all of these modules in the right form to be
  consumed by FrozenImporter.

The library that wishes to these modules make them available to the local
Python instance by extending `PyImport_FrozenModules` appropriately (see
https://docs.python.org/3/c-api/import.html#c.PyImport_FrozenModules).
"""

import argparse
import functools
import itertools
import marshal
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List


MAIN_INCLUDES = """#include <Python.h>

"""

MAIN_PREFIX = """
// Compiled standard library modules. These should be appended to the existing
// `PyImport_FrozenModules` that ships with CPython.
struct _frozen _PyImport_FrozenModules_torch[] = {
"""

FAKE_PREFIX = """
// Compiled standard library modules. These should be appended to the existing
// `PyImport_FrozenModules` that ships with CPython.
struct _frozen _PyImport_FrozenModules[] = {
"""

MAIN_SUFFIX = """\
    {0, 0, 0} /* sentinel */
};
"""

# Exclude some standard library modules to:
# 1. Slim down the final frozen lib.
# 2. Remove functionality we don't want to support.
DENY_LIST = [
    # Interface to unix databases
    "dbm",
    # ncurses bindings (terminal interfaces)
    "curses",
    # Tcl/Tk GUI
    "tkinter",
    "tkinter",
    # Tests for the standard library
    "test",
    "tests",
    "idle_test",
    "__phello__.foo.py",
    # importlib frozen modules. These are already baked into CPython.
    "_bootstrap.py",
    "_bootstrap_external.py",
]

NUM_BYTECODE_FILES = 5


def indent_msg(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        args[0].indent += 1
        ret = fn(*args, **kwargs)
        args[0].indent -= 1
        return ret

    return wrapper


@dataclass
class FrozenModule:
    # The fully qualified module name, e.g. 'foo.bar.baz'
    module_name: str
    # The name of the C variable that holds the bytecode, e.g. 'M_foo__bar__baz'
    c_name: str
    # The size of the C variable. Negative if this module is a package.
    size: int
    # The frozen bytecode
    bytecode: bytes


class Freezer:
    def __init__(self, verbose: bool):
        self.frozen_modules: List[FrozenModule] = []
        self.indent: int = 0
        self.verbose: bool = verbose

    def msg(self, path: Path, code: str):
        if not self.verbose:
            return
        # P: package dir
        # F: python file
        # S: skipped (not a package dir)
        # X: skipped (deny-listed)
        # N: skipped (not a python file)
        for i in range(self.indent):
            print("    ", end="")
        print(f"{code} {path}")

    def write_bytecode(self, install_root):
        """
        Write the `.c` files containing the frozen bytecode. Shard frozen
        modules evenly across the files.
        """
        bytecode_file_names = [
            f"bytecode_{i}.c" for i in range(NUM_BYTECODE_FILES)
        ]
        bytecode_files = [open(os.path.join(install_root, name), "w") for name in bytecode_file_names]
        it = itertools.cycle(bytecode_files)
        for m in self.frozen_modules:
            self.write_frozen(m, next(it))

        for f in bytecode_files:
            f.close()

    def write_main(self, install_root, oss):
        """
        Write the `main.c` file containing a table enumerating all the
        frozen modules.
        """
        with open(os.path.join(install_root, "main.c"), "w") as outfp:
            outfp.write(MAIN_INCLUDES)
            for m in self.frozen_modules:
                outfp.write(f"extern unsigned char {m.c_name}[];\n")

            outfp.write(MAIN_PREFIX)
            for m in self.frozen_modules:
                outfp.write(f'\t{{"{m.module_name}", {m.c_name}, {m.size}}},\n')
            outfp.write(MAIN_SUFFIX)
            if oss:
                outfp.write(FAKE_PREFIX)
                outfp.write(MAIN_SUFFIX)

    def write_frozen(self, m: FrozenModule, outfp):
        """
        Write a single frozen module's bytecode out to a C variable.
        """
        outfp.write(f"unsigned char {m.c_name}[] = {{")
        for i in range(0, len(m.bytecode), 16):
            outfp.write("\n\t")
            for c in bytes(m.bytecode[i : i + 16]):
                outfp.write("%d," % c)
        outfp.write("\n};\n")

    def compile_path(self, path: Path, top_package_path: Path):
        """Generic entry point for compiling a Path object."""
        if path.is_dir():
            self.compile_package(path, top_package_path)
        else:
            self.compile_file(path, top_package_path)

    @indent_msg
    def compile_package(self, path: Path, top_package_path: Path):
        """Compile all the files within a Python package dir."""
        assert path.is_dir()
        if path.name in DENY_LIST:
            self.msg(path, "X")
            return

        # Python packages are directories that have __init__.py in them.
        is_package_dir = any([child.name == "__init__.py" for child in path.iterdir()])
        if not is_package_dir:
            self.msg(path, "S")
            return

        self.msg(path, "P")
        # Recursively compile all children in this dir
        for child in path.iterdir():
            self.compile_path(child, top_package_path)

    def get_module_qualname(self, file_path: Path, top_package_path: Path) -> List[str]:
        # `path` looks like 'Lib/foo/bar/baz.py'

        # chop off 'Lib/' to get something that represents a Python module hierarchy.
        # e.g. 'foo/bar/baz.py', which maps to 'foo.bar.baz'
        normalized_path = file_path.relative_to(top_package_path.parent)

        if normalized_path.name == "__init__.py":
            # Special handling for `__init__.py`. In this case, this file
            # specifies that the containing directory should be treated as a package.
            # For 'foo/bar/baz/__init__.py':
            # - The module name is 'baz'
            module_basename = normalized_path.parent.name
            # - The parent is foo.bar (need to shave off the 'baz')
            module_parent = normalized_path.parent.parent.parts
        else:
            module_basename = normalized_path.stem
            module_parent = normalized_path.parent.parts
        return list(module_parent) + [module_basename]

    @indent_msg
    def compile_file(self, path: Path, top_package_path: Path):
        """
        Compile a Python source file to frozen bytecode. Append the result to
        `self.frozen_modules`.
        """
        assert path.is_file()
        if path.suffix != ".py":
            self.msg(path, "N")
            return

        if path.name in DENY_LIST:
            self.msg(path, "X")
            return

        self.msg(path, "F")
        module_qualname = self.get_module_qualname(path, top_package_path)
        module_mangled_name = "__".join(module_qualname)
        c_name = "M_" + module_mangled_name

        with open(path, "r") as src_file:
            co = compile(src_file.read(), path, "exec")

        bytecode = marshal.dumps(co)
        size = len(bytecode)
        if path.name == '__init__.py':
            # Python packages are signified by negative size.
            size = -size
        self.frozen_modules.append(
            FrozenModule(".".join(module_qualname), c_name, size, bytecode)
        )


parser = argparse.ArgumentParser(description="Compile py source")
parser.add_argument("paths", nargs="*", help="Paths to freeze.")
parser.add_argument("--verbose", action="store_true", help="Print debug logs")
parser.add_argument("--install_dir", help="Root directory for all output files")
parser.add_argument("--oss", action="store_true", help="If it's OSS build, add a fake _PyImport_FrozenModules")

args = parser.parse_args()

f = Freezer(args.verbose)

for p in args.paths:
    path = Path(p)
    if path.is_dir() and not Path.exists(path / '__init__.py'):
        # this 'top level path p' is a standard directory containing modules,
        # not a module itself
        # each 'mod' could be a dir containing __init__.py or .py file
        # NB: sorted to make sure this is deterministic
        for mod in sorted(path.glob("*")):
            f.compile_path(mod, mod)
    else:
        f.compile_path(path, path)

f.write_bytecode(args.install_dir)
f.write_main(args.install_dir, args.oss)
