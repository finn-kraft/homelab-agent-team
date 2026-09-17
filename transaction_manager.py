"""Transaction boundary shared by optional agent integrations."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Callable, Iterator


class TransactionManager:
    """Commit successful operations and roll back failures."""

    def __init__(self, connection_factory: Callable[[], Any]):
        self.connection_factory = connection_factory

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        connection = self.connection_factory()
        try:
            yield connection
            connection.commit()
        except BaseException:
            try:
                connection.rollback()
            finally:
                connection.close()
            raise
        else:
            connection.close()

    def run(self, operation: Callable[[Any], Any]) -> Any:
        with self.transaction() as connection:
            return operation(connection)


__all__ = ["TransactionManager"]
