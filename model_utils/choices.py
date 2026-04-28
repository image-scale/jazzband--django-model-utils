from __future__ import annotations

import copy
from typing import Any, Generic, Iterator, TypeVar

T = TypeVar("T")


class Choices(Generic[T]):
    """
    A class to define choices for Django model fields.

    Supports three formats:
    1. Simple string: 'DRAFT' -> (DRAFT, DRAFT, DRAFT)
    2. Two-tuple: ('DRAFT', 'is draft') -> (DRAFT, DRAFT, 'is draft')
    3. Three-tuple: (0, 'DRAFT', 'is draft') -> (0, DRAFT, 'is draft')

    Also supports option groups:
    ('group a', [('one', 'ONE'), ('two', 'TWO')])
    """

    def __init__(self, *choices: Any) -> None:
        self._choices: list[tuple[Any, str, Any]] = []
        self._display_map: dict[Any, Any] = {}
        self._identifier_map: dict[str, Any] = {}
        self._groups: list[tuple[str, list[tuple[Any, Any]]]] = []
        self._has_groups = False

        for choice in choices:
            self._add_choice(choice)

    def _add_choice(self, choice: Any) -> None:
        if isinstance(choice, str):
            # Simple string format: 'DRAFT' -> (DRAFT, DRAFT, DRAFT)
            self._choices.append((choice, choice, choice))
            self._display_map[choice] = choice
            self._identifier_map[choice] = choice
        elif isinstance(choice, (tuple, list)):
            if len(choice) == 1:
                raise ValueError(f"Invalid choice tuple length: {choice}")
            elif len(choice) == 2:
                first, second = choice
                if isinstance(second, (list, tuple)) and not isinstance(second, str):
                    # Option group: ('group name', [choices...])
                    self._has_groups = True
                    group_choices = []
                    for sub_choice in second:
                        if isinstance(sub_choice, str):
                            # String in group
                            self._choices.append((sub_choice, sub_choice, sub_choice))
                            self._display_map[sub_choice] = sub_choice
                            self._identifier_map[sub_choice] = sub_choice
                            group_choices.append((sub_choice, sub_choice))
                        elif len(sub_choice) == 2:
                            # (value, display) in group
                            val, display = sub_choice
                            self._choices.append((val, str(val) if not isinstance(val, str) else val, display))
                            self._display_map[val] = display
                            if isinstance(val, str):
                                self._identifier_map[val] = val
                            group_choices.append((val, display))
                        elif len(sub_choice) == 3:
                            # (db_val, identifier, display) in group
                            db_val, identifier, display = sub_choice
                            self._choices.append((db_val, identifier, display))
                            self._display_map[db_val] = display
                            self._identifier_map[identifier] = db_val
                            group_choices.append((db_val, display))
                    self._groups.append((first, group_choices))
                else:
                    # Two-tuple: (DRAFT, 'is draft') or ('DRAFT', 'is draft')
                    identifier, display = first, second
                    self._choices.append((identifier, identifier, display))
                    self._display_map[identifier] = display
                    self._identifier_map[identifier] = identifier
            elif len(choice) == 3:
                # Three-tuple: (0, 'DRAFT', 'is draft')
                db_value, identifier, display = choice
                self._choices.append((db_value, identifier, display))
                self._display_map[db_value] = display
                self._identifier_map[identifier] = db_value
            else:
                raise ValueError(f"Invalid choice tuple length: {choice}")
        else:
            raise ValueError(f"Invalid choice format: {choice}")

    def __getattr__(self, attname: str) -> T:
        if attname.startswith('_'):
            raise AttributeError(attname)
        if attname in self._identifier_map:
            return self._identifier_map[attname]  # type: ignore
        raise AttributeError(f"Choices has no attribute '{attname}'")

    def __len__(self) -> int:
        return len(self._choices)

    def __repr__(self) -> str:
        return "Choices" + repr(tuple(self._choices))

    def __iter__(self) -> Iterator[tuple[T, Any]]:
        if self._has_groups:
            for group_name, group_choices in self._groups:
                yield (group_name, group_choices)  # type: ignore
        else:
            for db_value, _, display in self._choices:
                yield (db_value, display)  # type: ignore

    def __reversed__(self) -> Iterator[tuple[T, Any]]:
        if self._has_groups:
            for group_name, group_choices in reversed(self._groups):
                yield (group_name, group_choices)  # type: ignore
        else:
            for db_value, _, display in reversed(self._choices):
                yield (db_value, display)  # type: ignore

    def __getitem__(self, key: T) -> Any:
        return self._display_map[key]

    def __contains__(self, item: T) -> bool:
        return item in self._display_map

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Choices):
            return self._choices == other._choices
        return False

    def __add__(self, other: object) -> Choices[T]:
        if isinstance(other, Choices):
            new = Choices[T]()
            new._choices = self._choices + other._choices
            new._display_map = {**self._display_map, **other._display_map}
            new._identifier_map = {**self._identifier_map, **other._identifier_map}
            return new
        elif isinstance(other, tuple):
            new = Choices[T]()
            new._choices = self._choices.copy()
            new._display_map = self._display_map.copy()
            new._identifier_map = self._identifier_map.copy()
            for choice in other:
                new._add_choice(choice)
            return new
        return NotImplemented

    def __radd__(self, other: object) -> Choices[T]:
        if isinstance(other, tuple):
            new = Choices[T]()
            for choice in other:
                new._add_choice(choice)
            new._choices = new._choices + self._choices
            new._display_map.update(self._display_map)
            new._identifier_map.update(self._identifier_map)
            return new
        return NotImplemented

    def __deepcopy__(self, memo: dict[Any, Any]) -> Choices[T]:
        new = Choices[T]()
        new._choices = copy.deepcopy(self._choices, memo)
        new._display_map = copy.deepcopy(self._display_map, memo)
        new._identifier_map = copy.deepcopy(self._identifier_map, memo)
        new._groups = copy.deepcopy(self._groups, memo)
        new._has_groups = self._has_groups
        return new

    def subset(self, *identifiers: str) -> Choices[T]:
        """Return a new Choices instance with only the specified identifiers."""
        new = Choices[T]()
        for identifier in identifiers:
            if identifier not in self._identifier_map:
                raise ValueError(f"'{identifier}' is not a valid identifier")
            db_value = self._identifier_map[identifier]
            # Find the full tuple
            for choice in self._choices:
                if choice[1] == identifier:
                    new._choices.append(choice)
                    new._display_map[db_value] = choice[2]
                    new._identifier_map[identifier] = db_value
                    break
        return new
