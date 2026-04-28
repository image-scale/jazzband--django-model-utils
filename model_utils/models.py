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
        value = timezone.now()
        setattr(model_instance, self.attname, value)
        return value


class TimeStampedModel(models.Model):
    """Abstract model with auto-updating created and modified timestamps."""

    created = AutoCreatedField()
    modified = AutoLastModifiedField()

    class Meta:
        abstract = True

    def save(self, *args: Any, **kwargs: Any) -> None:
        # Handle update_fields
        update_fields = kwargs.get('update_fields')

        # If saving for the first time
        if not self.pk:
            # For new objects, allow setting created and modified manually
            # but if modified is not set, use created value
            if self.created and not self.modified:
                self.modified = self.created
        else:
            # For existing objects, always update modified
            # unless update_fields is empty (which means skip the save entirely)
            if update_fields is not None:
                if len(update_fields) == 0:
                    # Empty update_fields means no actual save
                    super().save(*args, **kwargs)
                    return
                # Add 'modified' to update_fields
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

    @classmethod
    def _check_timeframed_conflict(cls) -> None:
        """Check for conflicting 'timeframed' attribute."""
        if hasattr(cls, 'timeframed'):
            attr = getattr(cls, 'timeframed')
            # If it's not a QueryManager, there's a conflict
            if not isinstance(attr, QueryManager):
                raise ImproperlyConfigured(
                    f"{cls.__name__} has a 'timeframed' attribute that conflicts "
                    "with TimeFramedModel's automatic manager."
                )

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if not cls._meta.abstract:
            # Check for conflicts
            for base in cls.__mro__:
                if base is TimeFramedModel:
                    continue
                if hasattr(base, 'timeframed') and not isinstance(getattr(base, 'timeframed', None), QueryManager):
                    for name, attr in base.__dict__.items():
                        if name == 'timeframed' and not isinstance(attr, QueryManager):
                            raise ImproperlyConfigured(
                                f"{cls.__name__} cannot define 'timeframed' as it conflicts "
                                "with TimeFramedModel's automatic manager."
                            )
            # Add the timeframed manager
            timeframed_manager = QueryManager(
                (Q(start__lte=timezone.now) | Q(start__isnull=True)) &
                (Q(end__gte=timezone.now) | Q(end__isnull=True))
            )
            timeframed_manager.auto_created = True
            cls.add_to_class('timeframed', timeframed_manager)


class StatusModel(models.Model):
    """Abstract model with status field and auto-updating status_changed timestamp."""

    status = StatusField()
    status_changed = MonitorField(monitor='status')

    class Meta:
        abstract = True

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if not cls._meta.abstract:
            # Add managers for each status choice
            status_choices = getattr(cls, 'STATUS', None)
            if status_choices is not None:
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
                        if hasattr(cls, identifier):
                            existing = getattr(cls, identifier)
                            if not isinstance(existing, QueryManager):
                                raise ImproperlyConfigured(
                                    f"{cls.__name__} cannot define '{identifier}' as it "
                                    "conflicts with StatusModel's automatic manager."
                                )

                        # Add the manager
                        manager = QueryManager(status=status_value)
                        manager.auto_created = True
                        cls.add_to_class(identifier, manager)

    def save(self, *args: Any, **kwargs: Any) -> None:
        update_fields = kwargs.get('update_fields')
        if update_fields is not None:
            update_fields = set(update_fields)
            # If status is being updated, also update status_changed
            if 'status' in update_fields:
                update_fields.add('status_changed')
                kwargs['update_fields'] = list(update_fields)
        super().save(*args, **kwargs)


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
