from __future__ import annotations

import warnings
from datetime import datetime
from typing import Any, ClassVar

from django.core.exceptions import ImproperlyConfigured
from django.db import models, router
from django.db.models import Q
from django.utils import timezone

from model_utils.fields import MonitorField, StatusField, UUIDField
from model_utils.managers import QueryManager, SoftDeletableManager


class AutoCreatedField(models.DateTimeField):
    """A DateTimeField that is set on first save."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault('editable', False)
        kwargs.setdefault('default', timezone.now)
        super().__init__(*args, **kwargs)


class AutoLastModifiedField(models.DateTimeField):
    """A DateTimeField that is updated on every save."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault('editable', False)
        kwargs.setdefault('default', timezone.now)
        super().__init__(*args, **kwargs)

    def pre_save(self, model_instance: models.Model, add: bool) -> datetime:
        # Check if this field was explicitly set (different from default)
        # by checking the _auto_now_override attribute
        if add:
            # First save - allow override if value was explicitly set
            # Check for explicit override marker
            override_marker = f'_override_{self.attname}'
            if getattr(model_instance, override_marker, False):
                # Value was explicitly set - use it
                delattr(model_instance, override_marker)
                return getattr(model_instance, self.attname)
            # Otherwise use created value if available to ensure equality
            created_field = None
            for field in model_instance._meta.fields:
                if isinstance(field, AutoCreatedField):
                    created_field = field
                    break
            if created_field:
                created_value = getattr(model_instance, created_field.attname)
                setattr(model_instance, self.attname, created_value)
                return created_value
        # Normal case - update to current time
        value = timezone.now()
        setattr(model_instance, self.attname, value)
        return value


class TimeStampedModel(models.Model):
    """Abstract model with auto-updating created and modified timestamps."""

    created = AutoCreatedField()
    modified = AutoLastModifiedField()

    class Meta:
        abstract = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # Check if 'modified' was explicitly passed
        if 'modified' in kwargs:
            self._override_modified = True
        if 'created' in kwargs:
            self._override_created = True
        super().__init__(*args, **kwargs)

    def save(self, *args: Any, **kwargs: Any) -> None:
        # Handle update_fields
        update_fields = kwargs.get('update_fields')

        # Check for empty update_fields (skip save case)
        if update_fields is not None and len(update_fields) == 0:
            # Empty update_fields means no actual save - bypass completely
            return

        # If this is the first save and created was explicitly set
        # but modified was not, set modified to match created
        if not self.pk:
            if hasattr(self, '_override_created') and not hasattr(self, '_override_modified'):
                self._override_modified = True
                self.modified = self.created

        # For existing objects with update_fields, ensure modified is included
        if self.pk and update_fields is not None:
            update_fields = set(update_fields)
            update_fields.add('modified')
            kwargs['update_fields'] = list(update_fields)

        super().save(*args, **kwargs)


class TimeFramedModel(models.Model):
    """Abstract model with start and end datetime fields."""

    start = models.DateTimeField(null=True, blank=True)
    end = models.DateTimeField(null=True, blank=True)

    class Meta:
        abstract = True


class TimeFramedManager(models.Manager['TimeFramedModel']):
    """Manager that filters to timeframed items (start <= now <= end)."""

    def get_queryset(self) -> models.QuerySet['TimeFramedModel']:
        now = timezone.now()
        return super().get_queryset().filter(
            (Q(start__lte=now) | Q(start__isnull=True)) &
            (Q(end__gte=now) | Q(end__isnull=True))
        )


def _add_timeframed_manager(sender: type, **kwargs: Any) -> None:
    """Add the timeframed manager to TimeFramedModel subclasses."""
    if not issubclass(sender, TimeFramedModel):
        return
    if sender._meta.abstract:
        return

    # Check for conflicts - look directly in class __dict__
    for base in sender.__mro__:
        if base is TimeFramedModel or base is models.Model:
            continue
        if 'timeframed' in base.__dict__:
            existing = base.__dict__['timeframed']
            if not isinstance(existing, (TimeFramedManager, QueryManager)):
                raise ImproperlyConfigured(
                    f"{sender.__name__} cannot define 'timeframed' as it conflicts "
                    "with TimeFramedModel's automatic manager."
                )

    # Add the timeframed manager if not already present
    if 'timeframed' not in sender.__dict__:
        timeframed_manager = TimeFramedManager()
        timeframed_manager.auto_created = True
        sender.add_to_class('timeframed', timeframed_manager)


models.signals.class_prepared.connect(_add_timeframed_manager)


class StatusModel(models.Model):
    """Abstract model with status field and auto-updating status_changed timestamp."""

    status = StatusField()
    status_changed = MonitorField(monitor='status')

    class Meta:
        abstract = True

    def save(self, *args: Any, **kwargs: Any) -> None:
        update_fields = kwargs.get('update_fields')
        if update_fields is not None:
            update_fields = set(update_fields)
            # If status is being updated, also update status_changed
            if 'status' in update_fields:
                update_fields.add('status_changed')
                kwargs['update_fields'] = list(update_fields)
        super().save(*args, **kwargs)


def _add_status_managers(sender: type, **kwargs: Any) -> None:
    """Add status managers for each status choice in StatusModel subclasses."""
    if not issubclass(sender, StatusModel):
        return
    if sender._meta.abstract:
        return

    # Get STATUS from class
    status_choices = getattr(sender, 'STATUS', None)
    if status_choices is None:
        return

    # Iterate over choices
    for choice in status_choices:
        if isinstance(choice, tuple) and len(choice) >= 2:
            # Get the status value (first element)
            status_value = choice[0]
            # For option groups, skip
            if isinstance(choice[1], (list, tuple)) and not isinstance(choice[1], str):
                continue
            # Get the identifier (status value or second element for 3-tuples)
            if len(choice) == 3:
                identifier = str(choice[1])
                status_value = choice[0]
            else:
                identifier = str(status_value)

            # Check for conflict
            if hasattr(sender, identifier):
                existing = getattr(sender, identifier)
                if not isinstance(existing, QueryManager):
                    raise ImproperlyConfigured(
                        f"{sender.__name__} cannot define '{identifier}' as it "
                        "conflicts with StatusModel's automatic manager."
                    )
                continue  # Already has a QueryManager

            # Add the manager
            manager = QueryManager(status=status_value)
            manager.auto_created = True
            sender.add_to_class(identifier, manager)


models.signals.class_prepared.connect(_add_status_managers)


class SoftDeletableModel(models.Model):
    """Abstract model with soft delete functionality."""

    is_removed = models.BooleanField(default=False)

    class Meta:
        abstract = True

    available_objects = SoftDeletableManager()

    class _DeprecatedObjectsManager(models.Manager['SoftDeletableModel']):
        def __init__(self) -> None:
            super().__init__()
            self._warned = False

        def all(self) -> Any:
            if not self._warned:
                warnings.warn(
                    "SoftDeletableModel.objects is deprecated, use available_objects instead",
                    DeprecationWarning,
                    stacklevel=2
                )
                self._warned = True
            return super().all()

        def get_queryset(self) -> Any:
            if not self._warned:
                warnings.warn(
                    "SoftDeletableModel.objects is deprecated, use available_objects instead",
                    DeprecationWarning,
                    stacklevel=2
                )
                self._warned = True
            return super().get_queryset()

    objects = _DeprecatedObjectsManager()

    def delete(
        self,
        using: str | None = None,
        soft: bool = True,
        *args: Any,
        **kwargs: Any
    ) -> tuple[int, dict[str, int]] | None:
        """Delete the object, either soft (default) or hard."""
        if soft:
            # Soft delete
            if using is None:
                using = router.db_for_write(self.__class__, instance=self)
            # Check if the connection exists
            from django.utils.connection import ConnectionDoesNotExist
            from django.db import connections
            if using not in connections:
                raise ConnectionDoesNotExist(f"The connection '{using}' doesn't exist")

            self.is_removed = True
            self.save(using=using, update_fields=['is_removed'])
            return None
        else:
            # Hard delete
            return super().delete(using=using, *args, **kwargs)


class UUIDModel(models.Model):
    """Abstract model with UUID as primary key."""

    id = UUIDField(primary_key=True, version=4)

    class Meta:
        abstract = True
