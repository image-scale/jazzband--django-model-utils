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
        # Create a copy of the FieldFile without copying the instance
        # This avoids the expensive deepcopy of the model instance
        if not value:
            return None
        # Get the state and remove the instance reference
        state = value.__getstate__()
        state['instance'] = None
        # Create a new FieldFile-like object that just stores the state
        class FieldFileCopy:
            def __init__(self, state: dict[str, Any], name: str) -> None:
                self._state = state
                self.name = name

            def __getstate__(self) -> dict[str, Any]:
                return self._state

            def __eq__(self, other: Any) -> bool:
                if isinstance(other, (FieldFile, FieldFileCopy)):
                    return self.name == getattr(other, 'name', None)
                return self.name == other

            def __ne__(self, other: Any) -> bool:
                return not self.__eq__(other)

            def __bool__(self) -> bool:
                return bool(self.name)

        return FieldFileCopy(state, value.name)
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

    def _is_field_deferred(self, field: str) -> bool:
        """Check if a field is deferred, handling both name and attname."""
        deferred = self.instance.get_deferred_fields()
        if field in deferred:
            return True
        # Check if the underlying field name is deferred (for attname tracking)
        model_field = self.field_map.get(field)
        if model_field and hasattr(model_field, 'name') and model_field.name in deferred:
            return True
        return False

    def get_field_value(self, field: str) -> Any:
        """Get the current value of a field."""
        if field in self.field_map:
            model_field = self.field_map[field]
            # For FK fields, handle specially to avoid triggering unnecessary queries
            if model_field and hasattr(model_field, 'is_relation') and model_field.is_relation:
                if model_field.many_to_one or model_field.one_to_one:
                    # Check if the object is cached (stored under field name in __dict__)
                    if field in self.instance.__dict__:
                        return self.instance.__dict__[field]
                    # Check if the field is actually deferred - if so, avoid query
                    if self._is_field_deferred(field):
                        # Return the ID if available, otherwise None
                        if model_field.attname in self.instance.__dict__:
                            return self.instance.__dict__[model_field.attname]
                        return None
                    # If ANY field is deferred, we're likely in a cascade situation
                    # Avoid triggering queries that could cause loops
                    if self.instance.get_deferred_fields():
                        if model_field.attname in self.instance.__dict__:
                            return self.instance.__dict__[model_field.attname]
                        return None
                    # For FieldInstanceTracker (not ModelInstanceTracker), prefer the ID
                    # to avoid unnecessary queries
                    if not isinstance(self, ModelInstanceTracker):
                        if model_field.attname in self.instance.__dict__:
                            return self.instance.__dict__[model_field.attname]
                    # Field is not deferred, we can access it normally
                    return getattr(self.instance, field)
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

            # Check if field is deferred (handles both name and attname)
            if self._is_field_deferred(field):
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

        # Check if field is deferred (handles both name and attname)
        if self._is_field_deferred(field):
            return False

        current = self.get_field_value(field)
        previous = self.saved_data.get(field)

        # Compare values
        return current != previous

    def previous(self, field: str) -> Any:
        """Return the previous saved value of a field."""
        if field not in self.fields:
            return None

        # If field is not in saved_data and instance has pk, fetch from DB
        if field not in self.saved_data and self.instance.pk:
            # Check if field is still deferred (handles both name and attname)
            if self._is_field_deferred(field):
                # Fetch the value from database for deferred field
                self.instance.refresh_from_db(fields=[field])
                value = self.get_field_value(field)
                self.saved_data[field] = _copy_field_value(value)
            else:
                # Field was never loaded or was assigned before accessing previous
                # Need to fetch the original value from database using only() to avoid
                # triggering tracker initialization
                try:
                    db_values = self.instance.__class__._default_manager.filter(
                        pk=self.instance.pk
                    ).values(field).first()
                    if db_values:
                        value = db_values[field]
                        self.saved_data[field] = _copy_field_value(value)
                except Exception:
                    pass

        return self.saved_data.get(field)

    def changed(self) -> dict[str, Any]:
        """Return a dict of fields that have changed and their previous values."""
        result = {}
        for field in self.fields:
            # Check if field is deferred (handles both name and attname)
            if self._is_field_deferred(field):
                continue
            if field in self.saved_data:
                current = self.get_field_value(field)
                if current != self.saved_data[field]:
                    result[field] = self.saved_data[field]
            elif self.instance.pk is None:
                # New instance - field has changed from None if it has any value
                current = self.get_field_value(field)
                # For new instances, any non-None current value means changed from None
                # Empty string counts as changed (e.g., for FileField or CharField)
                if current is not None:
                    result[field] = None
        return result

    def __enter__(self) -> FieldInstanceTracker:
        # Save current saved_data state with None to indicate tracking all fields
        self._context_stack.append((self.saved_data.copy(), None))
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        # Pop the saved state
        if self._context_stack:
            popped_state, my_fields = self._context_stack.pop()
            if not self._context_stack:
                # Outermost context exit - update baseline to current values
                self.set_saved_fields()
            # Nested context exit with no fields - don't update anything
            # since parent context will handle it

    def __call__(self, *fields: str) -> FieldTrackerContextManager:
        """Return a context manager that tracks specific fields."""
        return FieldTrackerContextManager(self, fields if fields else None)


class FieldTrackerContextManager:
    """Context manager for field-specific tracking within FieldInstanceTracker."""

    def __init__(self, tracker: FieldInstanceTracker, fields: tuple[str, ...] | Iterable[str] | None = None) -> None:
        self.tracker = tracker
        self.fields = set(fields) if fields else None
        self._entered = False

    def __enter__(self) -> FieldTrackerContextManager:
        # Push the current saved_data state AND the fields we're tracking to the stack
        self.tracker._context_stack.append((self.tracker.saved_data.copy(), self.fields))
        self._entered = True
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self.tracker._context_stack:
            popped_state, my_fields = self.tracker._context_stack.pop()

            if not self.tracker._context_stack:
                # Outermost context exit - update baseline to current values
                if my_fields:
                    # Only update specified fields
                    for f in my_fields:
                        if f in self.tracker.fields:
                            self.tracker.saved_data[f] = _copy_field_value(self.tracker.get_field_value(f))
                else:
                    self.tracker.set_saved_fields()
            else:
                # Nested context exit
                if my_fields:
                    # Find fields tracked ONLY by this context (not by parent contexts)
                    parent_tracked_fields: set[str] = set()
                    for _, parent_fields in self.tracker._context_stack:
                        if parent_fields:
                            parent_tracked_fields.update(parent_fields)
                        else:
                            # Parent tracks all fields
                            parent_tracked_fields = self.tracker.fields.copy()
                            break

                    # Update baselines for fields unique to this context
                    for f in my_fields:
                        if f not in parent_tracked_fields and f in self.tracker.fields:
                            self.tracker.saved_data[f] = _copy_field_value(self.tracker.get_field_value(f))
                # If no fields specified in nested context, don't update anything (restore state)
                # since parent context will handle it


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
                # For FK fields, use attname (e.g., 'fk_id') not name (e.g., 'fk')
                if hasattr(field, 'attname') and field.attname != field.name:
                    self.field_map[field.attname] = field
                else:
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

        # Wrap refresh_from_db to update saved_data after refresh
        self._wrap_refresh_from_db(sender)

        # Connect to post_init signal to set initial saved_data
        # Use weak=False to prevent garbage collection of the handler
        models.signals.post_init.connect(self.initialize_tracker, sender=sender, weak=False)
        # Connect to post_save signal to update saved_data
        models.signals.post_save.connect(self._post_save, sender=sender, weak=False)

        # Also connect signals for subclasses by listening to class_prepared
        # and connecting to each concrete subclass
        models.signals.class_prepared.connect(
            self._connect_subclass_signals, weak=False
        )

    def _connect_subclass_signals(self, sender: type, **kwargs: Any) -> None:
        """Connect signals for subclasses of the tracked model."""
        if self.model_class is None:
            return
        # Check if sender is a subclass of our model (but not the model itself)
        if issubclass(sender, self.model_class) and sender is not self.model_class:
            if not sender._meta.abstract:
                # Connect signals for this subclass
                models.signals.post_init.connect(self.initialize_tracker, sender=sender, weak=False)
                models.signals.post_save.connect(self._post_save, sender=sender, weak=False)
                # Also wrap refresh_from_db for the subclass
                self._wrap_refresh_from_db(sender)

    def _wrap_refresh_from_db(self, sender: type) -> None:
        """Wrap refresh_from_db to update tracker after refresh."""
        original_refresh = sender.refresh_from_db

        # Don't wrap if already wrapped by this tracker or another
        if hasattr(original_refresh, '_is_tracker_wrapped'):
            return

        tracker_descriptor = self

        @functools.wraps(original_refresh)
        def refresh_from_db_wrapper(self: models.Model, using: str | None = None, fields: list[str] | None = None, from_queryset: Any = None) -> None:
            original_refresh(self, using=using, fields=fields, from_queryset=from_queryset)
            # Update the tracker's saved_data for the refreshed fields
            tracker = tracker_descriptor.__get__(self, type(self))
            if isinstance(tracker, FieldInstanceTracker) and not tracker._context_stack:
                if fields:
                    # Only update the fields that were refreshed
                    tracker.set_saved_fields(set(fields) & tracker.fields)
                else:
                    # All fields were refreshed
                    tracker.set_saved_fields()

        refresh_from_db_wrapper._is_tracker_wrapped = True  # type: ignore
        sender.refresh_from_db = refresh_from_db_wrapper  # type: ignore

    def initialize_tracker(self, sender: models.Model, instance: models.Model, **kwargs: Any) -> None:
        """Initialize the tracker on a new instance."""
        tracker = self.__get__(instance, type(instance))
        if isinstance(tracker, FieldInstanceTracker):
            # Only set saved fields for existing instances (loaded from DB)
            # For new instances, saved_data should be empty so previous() returns None
            if instance.pk is not None:
                tracker.set_saved_fields()

    def _post_save(self, sender: type, instance: models.Model, created: bool, update_fields: list[str] | frozenset[str] | None = None, **kwargs: Any) -> None:
        """Update saved_data after a save."""
        tracker = self.__get__(instance, type(instance))
        if isinstance(tracker, FieldInstanceTracker):
            # Don't update saved_data if we're inside a context manager
            if tracker._context_stack:
                return
            if update_fields:
                # Only update the saved fields that were actually saved
                tracker.set_saved_fields(set(update_fields) & tracker.fields)
            else:
                tracker.set_saved_fields()

    def __call__(self, *args: Any, fields: Iterable[str] | None = None) -> Any:
        """Return a decorator or context manager.

        Can be used as:
        - @Tracked.tracker - decorator with no arguments
        - @Tracked.tracker(fields=['name']) - decorator with fields argument
        """
        # If called with a callable as first argument and no fields, it's a decorator
        if len(args) == 1 and callable(args[0]) and fields is None:
            # Called as @Tracked.tracker with no arguments
            func = args[0]
            @functools.wraps(func)
            def wrapper(*wrapper_args: Any, **wrapper_kwargs: Any) -> Any:
                if wrapper_args:
                    instance = wrapper_args[0]
                    tracker = self.__get__(instance, type(instance))
                    if isinstance(tracker, FieldInstanceTracker):
                        with tracker:
                            return func(*wrapper_args, **wrapper_kwargs)
                return func(*wrapper_args, **wrapper_kwargs)
            return wrapper

        # Otherwise return a decorator that tracks specific fields
        track_fields = list(fields) if fields else list(args) if args else None
        return TrackerDecorator(self, tuple(track_fields) if track_fields else None)


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
                    ctx = FieldTrackerContextManager(tracker_instance, self.fields)
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
        # If instance is new (not yet saved), all fields are considered changed
        if self.instance.pk is None:
            return True

        if field not in self.fields:
            raise FieldError(f"'{field}' is not a tracked field")

        # Check if field is deferred (handles both name and attname)
        if self._is_field_deferred(field):
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
            # Check if field is deferred (handles both name and attname)
            if self._is_field_deferred(field):
                continue
            if field in self.saved_data:
                current = self.get_field_value(field)
                if current != self.saved_data[field]:
                    result[field] = self.saved_data[field]
        return result


class ModelTracker(FieldTracker):
    """Class-level descriptor that provides different change tracking behavior."""

    tracker_class = ModelInstanceTracker
