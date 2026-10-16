"""Compatibility imports for existing scripts; the application now lives in mica."""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.util
import sys
from types import CodeType, ModuleType
from typing import Any


class _LegacyImports(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, fullname: str, path: Any = None, target: ModuleType | None = None) -> Any:
        if not fullname.startswith("pistachio."):
            return None
        current = importlib.util.find_spec("mica" + fullname[len("pistachio") :])
        if current is None:
            return None
        return importlib.util.spec_from_loader(
            fullname,
            self,
            origin=current.origin,
            is_package=current.submodule_search_locations is not None,
        )

    def create_module(self, spec: Any) -> ModuleType:
        return importlib.import_module("mica" + spec.name[len("pistachio") :])

    def exec_module(self, module: ModuleType) -> None:
        pass

    def get_code(self, fullname: str) -> CodeType:
        current = importlib.util.find_spec("mica" + fullname[len("pistachio") :])
        assert current and isinstance(current.loader, importlib.abc.InspectLoader)
        code = current.loader.get_code(current.name)
        assert code is not None
        return code


sys.meta_path.insert(0, _LegacyImports())
