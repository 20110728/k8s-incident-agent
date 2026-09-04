from backend.app.persistence.database import connect_database
from backend.app.persistence.migrations import run_migrations
from backend.app.persistence.settings import get_database_settings


def main() -> None:
    settings = get_database_settings()

    with connect_database(settings) as connection:
        applied_versions = run_migrations(connection)

    if applied_versions:
        rendered = ", ".join(
            str(version)
            for version in applied_versions
        )
        print(f"Applied persistence migrations: {rendered}")
    else:
        print("Persistence schema is already up to date")


if __name__ == "__main__":
    main()