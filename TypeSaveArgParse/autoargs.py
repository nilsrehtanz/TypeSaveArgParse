import dataclasses
import sys
import types
from dataclasses import Field, asdict, dataclass, field
from enum import Enum
from functools import partial
from inspect import signature
from pathlib import Path
from typing import get_args, get_origin

import ruamel.yaml
from configargparse import ArgumentParser

try:
    from docstring_parser import parse as doc_parse
except Exception:
    doc_parse = None

from TypeSaveArgParse.utils import (
    cast_all,
    cast_if_enum,
    cast_if_list_to,
    class_to_str,
    extract_sub_annotation,
    len_checker,
    translation_enum_to_str,
)

config_help = "config file path"


def data_class_to_arg_parse(
    cls,
    parser: None | ArgumentParser = None,
    default_config=None,
    _addendum: str = "",
    _checks=None,
    _enum=None,
    _loop_detection=None,
    _class_mapping=None,
    _help=None,
):
    """
    Converts a data class into ArgumentParser arguments.

    Args:
        cls: The data class to convert into ArgumentParser arguments.
        parser (ArgumentParser or None): An existing ArgumentParser instance. If None, a new ArgumentParser will be created.
        default_config: Default configuration file.
        _addendum (str): A string to prepend to each argument's name.
        _checks: Internal parameter for checking arguments.
        _enum: Internal parameter for handling Enum types.
        _loop_detection: Internal parameter for detecting recursive data classes.
        _class_mapping: Internal parameter for mapping class names.

    Returns:
        ArgumentParser, dict: The ArgumentParser instance with added arguments, and a dictionary mapping class names to classes.
    """
    if _help is None:
        _help = {}

    # Default values
    if _class_mapping is None:
        _class_mapping = {}
    _loop_detection = [cls] if _loop_detection is None else [*_loop_detection, cls]
    if _enum is None:
        _enum = {}
    if _checks is None:
        _checks = {}
    if parser is None:
        # extend existing arg_parsers
        p: ArgumentParser = ArgumentParser()
        p.add_argument("-config", "--config", is_config_file_arg=True, default=default_config, type=str, help=config_help)
    else:
        p = parser
    if doc_parse is not None:
        doc_str = doc_parse(cls.__doc__)
        st = doc_str.long_description
        p.add_help(st) if st is not None else None  # type: ignore
        for a in doc_str.params:
            _help[_addendum + a.arg_name] = a.description
        # print(_help)
    # fetch the constructor's signature
    parameters = signature(cls).parameters
    cls_fields = sorted(set(parameters))

    # split the kwargs into native ones and new ones
    for name in cls_fields:
        dict_name = _addendum + name
        key = "--" + _addendum + name
        default = parameters[name].default
        annotation = parameters[name].annotation
        # Handling :A |B |...| None (None means Optional argument)
        annotations = []
        if get_origin(annotation) == types.UnionType:
            for i in get_args(annotation):
                if i == types.NoneType:
                    can_be_none = True
                else:
                    annotations.append(i)
                    annotation = i
        if len(set(annotations)) > 1:
            raise NotImplementedError("UnionType", annotations)  # TODO
        del annotations
        # Handling :bool = [True | False]
        if annotation == bool:
            p.add_argument(key, action="store_false" if default else "store_true", default=default, help=_help.get(dict_name, None))
            continue
        # Handling :subclass of Enum
        elif isinstance(default, Enum) or issubclass(annotation, Enum):
            _enum[dict_name] = annotation
            p.add_argument(key, default=default, choices=translation_enum_to_str(annotation), help=_help.get(dict_name, None))
        # Handling :list,tuple,set
        elif get_origin(annotation) in (list, tuple, set):
            # Unpack Sequence[...] -> ...
            org_annotation = annotation
            annotation, annotations, had_ellipsis, can_be_none = extract_sub_annotation(annotation)
            if get_origin(org_annotation) == tuple and not had_ellipsis:
                # Tuple with fixed size
                _checks[dict_name] = partial(
                    len_checker, num_elements=len(annotations), org_annotation=org_annotation, can_be_none=can_be_none, name=name
                )
            if len(set(annotations)) != 1:
                raise NotImplementedError("Non uniform sequence", annotations)
            elif issubclass(annotation, Enum):  # List of Enums
                choices = [f for f in dir(annotation) if not f.startswith("__")]
                p.add_argument(key, nargs="+", default=default, type=str, help=_help.get(dict_name, "List of keys"), choices=choices)
                _enum[dict_name] = annotation
            else:  # All other Lists
                p.add_argument(
                    key, nargs="+", default=default, type=annotation, help=_help.get(dict_name, "List of " + class_to_str(annotation))
                )
        elif dataclasses.is_dataclass(annotation):
            if annotation in _loop_detection:
                raise ValueError("RECURSIVE DATACLASS", annotation)
            cls_name = dict_name + "."  # class_to_str(annotation).split(".")[-1] + "."
            _class_mapping[cls_name] = annotation
            p, _class_mapping = data_class_to_arg_parse(
                cls=annotation,
                parser=p,
                default_config=default_config,
                _addendum=cls_name,
                _checks=_checks,
                _enum=_enum,
                _loop_detection=_loop_detection,
                _class_mapping=_class_mapping,
                _help=_help,
            )
        else:
            # Rest like int,float,str,Path
            p.add_argument(key, default=default, type=annotation, help=_help.get(dict_name, None))
    return p, _class_mapping


def convert_obj_to_yaml(self, data: ruamel.yaml.CommentedMap, _addendum: str = ""):
    parameters = signature(self.__class__).parameters
    pref = None
    for k, v in asdict(self).items():
        k_full = _addendum + k
        att = getattr(self, k)

        if k.startswith("_"):
            continue
        if v is None:
            s = f"{k_full}: {att!s} # {parameters[k].annotation}\n"
            data.yaml_set_comment_before_after_key(pref, before=s, indent=0)
            continue
        if isinstance(v, (list, set, tuple)) and len(v) == 0:
            s = f"{k_full}: {att!s} # {parameters[k].annotation}\n"
            data.yaml_set_comment_before_after_key(pref, before=s, indent=0)
            continue
        if isinstance(v, (list, set, tuple)):

            def enum_to_str(i):
                if isinstance(i, Enum):
                    return i.name
                return i

            v = [enum_to_str(i) for i in v]  # noqa: PLW2901
        if isinstance(v, Enum):
            v = v.name  # noqa: PLW2901
        elif isinstance(v, Path):
            v = str(v)  # noqa: PLW2901
        elif isinstance(v, set):
            v = list(v)  # noqa: PLW2901
        elif dataclasses.is_dataclass(att):
            convert_obj_to_yaml(att, data, _addendum=k_full + ".")
            # TODO Recursive printing
            continue
        pref = k_full
        data[k_full] = v
        # fetch the constructor's signature
        # if len(start_comment) != 0:
        #    data.yaml_set_start_comment(start_comment)


def add_comments_to_yaml(cls, data: ruamel.yaml.CommentedMap, _addendum: str = "", _loop_detection=None):
    ### Add Comments ###
    # split the kwargs into native ones and new ones
    _loop_detection = [cls] if _loop_detection is None else [*_loop_detection, cls]
    parameters = signature(cls).parameters
    cls_fields = sorted(set(parameters))

    for name in cls_fields:
        full_name = _addendum + name
        default = parameters[name].default
        default = "" if str(default) == "<factory>" else f"- default [{default}]"
        annotation = parameters[name].annotation

        # Handling :A |B |...| None (None means Optional argument)
        annotations = []
        if get_origin(annotation) == types.UnionType:
            for i in get_args(annotation):
                if i != types.NoneType:
                    annotations.append(i)
                    annotation = i
        if len(annotations) > 1:
            continue
        del annotations
        # Handling :bool = [True | False]
        if annotation == bool:
            data.yaml_add_eol_comment(f"[True|False] {default}", full_name)

        # Handling :subclass of Enum
        elif isinstance(default, Enum) or issubclass(annotation, Enum):
            s = f"{annotation} Choices:{translation_enum_to_str(annotation)} {default}"
            data.yaml_add_eol_comment(s, full_name)
        ## Handling :list,tuple,set
        elif get_origin(annotation) in (list, tuple, set):
            # Unpack Sequence[...] -> ...
            org_annotation = annotation
            annotation, annotations, had_ellipsis, can_be_none = extract_sub_annotation(annotation)
            num_ann = len(annotations)
            if get_origin(org_annotation) == tuple and not had_ellipsis:
                s = f"Note: Tuple with a fixed size of {num_ann}"
                data.yaml_set_comment_before_after_key(full_name, before=s)

            if issubclass(annotation, Enum):
                s = f"{annotation} Choices:{translation_enum_to_str(annotation)} {default}"
                data.yaml_add_eol_comment(s, full_name)

        elif dataclasses.is_dataclass(annotation):
            if annotation in _loop_detection:
                raise ValueError("RECURSIVE DATACLASS", annotation)
            add_comments_to_yaml(cls=annotation, data=data, _addendum=full_name + ".", _loop_detection=_loop_detection)


@dataclass()
class Class_to_ArgParse:
    @classmethod
    def get_opt(cls, parser: None | ArgumentParser = None, default_config=None):
        _checks = {}
        _enum = {}

        p, _class_mapping = data_class_to_arg_parse(cls, parser, default_config, _checks=_checks, _enum=_enum)
        out = cls.from_kwargs(**p.parse_args().__dict__, _checks=_checks, _enum=_enum, _class_mapping=_class_mapping)
        return out

    @classmethod
    def from_kwargs(cls, _checks=None, _enum=None, _class_mapping=None, **kwargs):
        # fetch the constructor's signature
        if _class_mapping is None:
            _class_mapping = {}
        sub_class_attributes = {a: [] for a, _ in _class_mapping.items()}
        _dots = sorted([(-a.count("."), a) for a in _class_mapping])  # remember how many indirection tupel(inderiction, name)
        if _enum is None:
            _enum = {}
        if _checks is None:
            _checks = {}
        parameters = signature(cls).parameters
        cls_fields = set(parameters)
        # split the kwargs into native ones and new ones
        native_args, new_args = {}, {}
        for name, val2 in kwargs.items():
            if name == "config":
                continue
            val = val2
            skip_rest = False
            for subclass_name, subclass_att in sub_class_attributes.items():
                if name.startswith(subclass_name):
                    sub_key = name.replace(subclass_name, "")
                    if "." not in sub_key:
                        val = cast_all(val, signature(_class_mapping[subclass_name]).parameters[sub_key], _enum.get(name, None))
                        subclass_att.append((sub_key, val))
                        skip_rest = True
                        break
            if skip_rest:
                continue
            if name in cls_fields:
                # recursive call on list HERE
                val = cast_all(val, parameters[name], _enum.get(name, None))
                native_args[name] = val
            else:
                # unknown parameters
                raise NotImplementedError(name, val)
                new_args[name] = val
            _checks.get(name, id)(val)
        for _, name in _dots:
            att = sub_class_attributes[name]
            _cls = _class_mapping[name]
            obj = _cls(**dict(att))
            if str(name).count(".") == 1:
                native_args[str(name).replace(".", "")] = obj
            else:
                top_lvl, k = str(name[:-1]).rsplit(".", 1)
                sub_class_attributes[top_lvl + "."].append((k, obj))

        ret = cls(**native_args)
        # ... and add the new ones by hand
        for new_name, new_val in new_args.items():
            setattr(ret, new_name, new_val)
        return ret

    def __getstate__(self):
        """Replace fields, so that they can be pickled"""
        state = self.__dict__.copy()
        for key, value in state.items():
            if isinstance(value, Field):
                if isinstance(value.default, dataclasses._MISSING_TYPE):
                    state[key] = value.default_factory()  # type: ignore

                else:
                    state[key] = value.default
                self.__dict__[key] = state[key]
        return state

    def save_config(self, outfile: str | Path, default_flow_style: None | bool = None):
        import ruamel.yaml as ryaml
        # import yaml

        with open(outfile, "w") as out_file_stream:
            y = ryaml.YAML()  # typ="safe", pure=True
            y.default_flow_style = default_flow_style
            data = ruamel.yaml.CommentedMap()
            convert_obj_to_yaml(self, data)
            add_comments_to_yaml(self.__class__, data)
            y.dump(data, out_file_stream)
        # Could not find to use "" for strings instead of ''.
        # So we have this solution
        # Thanks I hate it. (pyYaml need "" or it would cas 'x' to "'x'" instead of "x")
        with open(outfile) as out_file_stream:
            data = out_file_stream.read()
        with open(outfile, "w") as out_file_stream:
            out_file_stream.write(data.replace("'", '"'))


# data = ruamel.yaml.round_trip_load(outfile)

# data["test1"].yaml_set_start_comment("before test2", indent=2)
# data["test1"]["test2"].yaml_set_start_comment("after test2", indent=4)
# ruamel.yaml.round_trip_dump(data, sys.stdout)


# ryaml.safe_dump(asdict(self), outfile, default_flow_style=default_flow_style, sort_keys=False)
