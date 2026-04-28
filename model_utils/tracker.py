from __future__ import annotations

import copy
import functools
from typing import Any, Callable, Iterable, TypeVar

from django.core.exceptions import FieldError
from django.db import models
from django.db.models.fields.files import FieldFile

ModelT = TypeVar("ModelT", bound=models.Model)


def _copy_field_value(value: Any) -> Any:
    """Deep copy a field value, with special handling for FieldFile."""
    if isinstance(value, FieldFile):
        # Don't copy the instance - it causes issues
        state = value.__getstate__()
        state['instance'] = None
        new_file = FieldFile(None, value.field)
        new_file.__setstate__(state)
        return new_file
    try:
        return copy.deepcopy(value)
    except Exception:
        return value


class DescriptorWrapper:
    """Wrapper around a model field descriptor to support deferred field tracking."""

    def __init__(self, field: models.Field[Any, Any], tracker_attname: str, field_name: str) -> None:
        self.field = field
        self.tracker_attname = tracker_attname
        self.field_name = field_name
        # Get the original descriptor (usually DeferredAttribute)
        self.original_descriptor = field.descriptor_class(field)

    def __get__(self, obj: models.Model | None, objtype: type | None = None) -> Any:
        if obj is None:
            return self
        # Use the original descriptor's __get__ method
        return self.original_descriptor.__get__(obj, objtype)

    def __set__(self, obj: models.Model, value: Any) -> None:
        # Use the original descriptor's __set__ if it has one
        if hasattr(self.original_descriptor, '__set__'):
            self.original_descriptor.__set__(obj, value)
        else:
            obj.__dict__[self.field_name] = value

        # If this was a deferred field, it's no longer deferred after assignment
        # Update the tracker's saved_data if the field was deferred
        tracker = getattr(obj, self.tracker_attname, None)
        if tracker is not None and self.field_name in obj.get_deferred_fields():
            # Field was deferred; fetch and store the previous value
            pass

    def __delete__(self, obj: models.Model) -> None:
        if hasattr(self.original_descriptor, '__delete__'):
            self.original_descriptor.__delete__(obj)
        else:
            del obj.__dict__[self.field_name]


class FieldInstanceTracker:
    """Instance-level tracker that tracks field changes on a model instance."""

    def __init__(self, instance: models.Model, fields: Iterable[str], field_map: dict[str, models.Field[Any, Any]]) -> None:
        self.instance = instance
        self.fields = set(fields)
        self.field_map = field_map
        self.saved_data: dict[str, Any] = {}
        self._context_stack: list[dict[str, Any]] = []

    def get_field_value(self, field: str) -> Any:
        """Get the current value of a field."""
        if field in self.field_map:
            # It's a real field
            return getattr(self.instance, field)
        else:
            # It might be a property or attname (like fk_id)
            return getattr(self.instance, field, None)

    def set_saved_fields(self, fields: Iterable[str] | None = None) -> None:
        """Save the current values of tracked fields."""
        if fields is None:
            fields = self.fields

        for field in fields:
            if field not in self.fields:
                continue

            # Check if field is deferred
            deferred = self.instance.get_deferred_fields()
            if field in deferred:
                # Don't try to access deferred fields - wait until they're accessed
                continue

            value = self.get_field_value(field)
            self.saved_data[field] = _copy_field_value(value)

    def current(self, fields: Iterable[str] | None = None) -> dict[str, Any]:
        """Return the current values of tracked fields."""
        if fields is None:
            fields = self.fields
        result = {}
        for field in fields:
            if field not in self.fields:
                continue
            result[field] = self.get_field_value(field)
        return result

    def has_changed(self, field: str) -> bool:
        """Check if a field has changed since last save."""
        if field not in self.fields:
            raise FieldError(f"'{field}' is not a tracked field")

        # Check if field is deferred
        deferred = self.instance.get_deferred_fields()
        if field in deferred:
            return False

        current = self.get_field_value(field)
        previous = self.saved_data.get(field)

        # Compare values
        return current != previous

    def previous(self, field: str) -> Any:
        """Return the previous saved value of a field."""
        if field not in self.fields:
            return None

        # If field is deferred and we don't have saved data, fetch from DB
        if field not in self.saved_data:
            deferred = self.instance.get_deferred_fields()
            if field in deferred and self.instance.pk:
                # Fetch the value from database
                self.instance.refresh_from_db(fields=[field])
                value = self.get_field_value(field)
                self.saved_data[field] = _copy_field_value(value)

        return self.saved_data.get(field)

    def changed(self) -> dict[str, Any]:
        """Return a dict of fields that have changed and their previous values."""
        result = {}
        for field in self.fields:
            deferred = self.instance.get_deferred_fields()
            if field in deferred:
                continue
            if field in self.saved_data:
                current = self.get_field_value(field)
                if current != self.saved_data[field]:
                    result[field] = self.saved_data[field]
            elif self.instance.pk is None:
                # New instance - field has changed from None
                current = self.get_field_value(field)
                if current != '' and current is not None:
                    result[field] = None
                elif field == 'name' and current == '':
                    # CharField with blank default
                    result[field] = None
        return result

    def __enter__(self) -> FieldInstanceTracker:
        # Save current saved_data state
        self._context_stack.append(self.saved_data.copy())
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        # Restore saved_data from before the context
        if self._context_stack:
            self.saved_data = self._context_stack.pop()


class FieldTracker:
    """Class-level descriptor that provides field change tracking."""

    tracker_class = FieldInstanceTracker

    def __init__(self, fields: Iterable[str] | None = None) -> None:
        self.fields = set(fields) if fields else None
        self.attname: str = ""
        self.field_map: dict[str, models.Field[Any, Any]] = {}
        self.model_class: type | None = None

    def __get__(self, instance: models.Model | None, owner: type | None = None) -> FieldTracker | FieldInstanceTracker:
        if instance is None:
            return self

        tracker_attname = f'_tracker_{self.attname}'
        tracker = getattr(instance, tracker_attname, None)
        if tracker is None:
            # Get the fields to track
            fields = self.fields if self.fields else set(self.field_map.keys())
            tracker = self.tracker_class(instance, fields, self.field_map)
            setattr(instance, tracker_attname, tracker)
        return tracker

    def contribute_to_class(self, cls: type, name: str) -> None:
        self.attname = name
        self.model_class = cls
        setattr(cls, name, self)
        models.signals.class_prepared.connect(self.finalize_class, sender=cls)

    def finalize_class(self, sender: type, **kwargs: Any) -> None:
        """Called when the model class is fully prepared."""
        # Build the field map
        self.field_map = {}
        opts = sender._meta

        if self.fields:
            # Only track specified fields
            for field_name in self.fields:
                # Check if it's a field name or attname (like fk_id)
                field = None
                for f in opts.fields:
                    if f.name == field_name or f.attname == field_name:
                        field = f
                        break
                if field:
                    self.field_map[field_name] = field
                else:
                    # Could be a property - still track it
                    self.field_map[field_name] = None  # type: ignore
        else:
            # Track all fields
            for field in opts.fields:
                self.field_map[field.name] = field

        # Wrap descriptors for deferred field support
        for field_name, field in self.field_map.items():
            if field and hasattr(sender, field_name):
                current_attr = getattr(sender, field_name, None)
                # Only wrap if it's a DeferredAttribute or similar
                from django.db.models.query_utils import DeferredAttribute
                if isinstance(current_attr, DeferredAttribute):
                    wrapper = DescriptorWrapper(field, f'_tracker_{self.attname}', field_name)
                    setattr(sender, field_name, wrapper)

        # Connect to post_init signal to set initial saved_data
        models.signals.post_init.connect(self.initialize_tracker, sender=sender)
        # Connect to post_save signal to update saved_data
        models.signals.post_save.connect(self._post_save, sender=sender)
        # Connect to post_refresh_from_db to update saved_data
        # This requires a custom approach since there's no built-in signal

    def initialize_tracker(self, sender: models.Model, instance: models.Model, **kwargs: Any) -> None:
        """Initialize the tracker on a new instance."""
        tracker = self.__get__(instance, type(instance))
        if isinstance(tracker, FieldInstanceTracker):
            tracker.set_saved_fields()

    def _post_save(self, sender: type, instance: models.Model, created: bool, update_fields: list[str] | frozenset[str] | None = None, **kwargs: Any) -> None:
        """Update saved_data after a save."""
        tracker = self.__get__(instance, type(instance))
        if isinstance(tracker, FieldInstanceTracker):
            if update_fields:
                # Only update the saved fields that were actually saved
                tracker.set_saved_fields(set(update_fields) & tracker.fields)
            else:
                tracker.set_saved_fields()

    def __call__(self, *fields: str) -> TrackerContextManager:
        """Return a context manager that tracks specific fields."""
        # This is called as a decorator or context manager
        # When called on the class descriptor, we need to return something that
        # can be used as a decorator
        if self.model_class and len(fields) == 1 and callable(fields[0]):
            # Called as @Tracked.tracker with no arguments
            func = fields[0]
            @functools.wraps(func)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                if args:
                    instance = args[0]
                    tracker = self.__get__(instance, type(instance))
                    if isinstance(tracker, FieldInstanceTracker):
                        with tracker:
                            return func(*args, **kwargs)
                return func(*args, **kwargs)
            return wrapper  # type: ignore
        # Return a TrackerContextManager-like object for use with instance
        return TrackerDecorator(self, fields if fields else None)


class TrackerDecorator:
    """Helper for @Tracker decorator syntax."""

    def __init__(self, tracker: FieldTracker, fields: tuple[str, ...] | None = None) -> None:
        self.tracker = tracker
        self.fields = fields

    def __call__(self, func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if args:
                instance = args[0]
                tracker_instance = self.tracker.__get__(instance, type(instance))
                if isinstance(tracker_instance, FieldInstanceTracker):
                    ctx = TrackerContextManager(tracker_instance, self.fields)
                    with ctx:
                        return func(*args, **kwargs)
            return func(*args, **kwargs)
        return wrapper


class TrackerContextManager:
    """Context manager for field tracking within a specific scope."""

    def __init__(self, tracker: FieldInstanceTracker, fields: Iterable[str] | None = None) -> None:
        self.tracker = tracker
        self.fields = set(fields) if fields else None
        self._saved_state: dict[str, Any] | None = None

    def __enter__(self) -> TrackerContextManager:
        # Save current state
        if self.fields:
            self._saved_state = {f: self.tracker.saved_data.get(f) for f in self.fields if f in self.tracker.fields}
        else:
            self._saved_state = self.tracker.saved_data.copy()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        # Restore saved state and reset to current values
        if self._saved_state is not None:
            if self.fields:
                for f in self.fields:
                    if f in self.tracker.fields:
                        self.tracker.saved_data[f] = _copy_field_value(self.tracker.get_field_value(f))
            else:
                self.tracker.set_saved_fields()

    def __call__(self, func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with self:
                return func(*args, **kwargs)
        return wrapper


class ModelInstanceTracker(FieldInstanceTracker):
    """Instance-level tracker that tracks changes differently from FieldInstanceTracker.

    ModelTracker considers all fields as changed before the first save.
    """

    def has_changed(self, field: str) -> bool:
        """Check if a field has changed since last save."""
        if field not in self.fields:
            # For ModelTracker, unknown fields are considered changed if instance is new
            if self.instance.pk is None:
                return True
            return False

        # If instance is new (not yet saved), all fields are considered changed
        if self.instance.pk is None:
            return True

        # Check if field is deferred
        deferred = self.instance.get_deferred_fields()
        if field in deferred:
            return False

        current = self.get_field_value(field)
        previous = self.saved_data.get(field)

        return current != previous

    def changed(self) -> dict[str, Any]:
        """Return a dict of fields that have changed and their previous values.

        For ModelTracker, returns empty dict before first save.
        """
        if self.instance.pk is None:
            # Before first save, ModelTracker returns empty changed() dict
            return {}

        result = {}
        for field in self.fields:
            deferred = self.instance.get_deferred_fields()
            if field in deferred:
                continue
            if field in self.saved_data:
                current = self.get_field_value(field)
                if current != self.saved_data[field]:
                    result[field] = self.saved_data[field]
        return result


class ModelTracker(FieldTracker):
    """Class-level descriptor that provides different change tracking behavior."""

    tracker_class = ModelInstanceTracker
