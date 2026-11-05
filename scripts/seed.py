"""Create the initial clients, profiles and users (idempotent).

    python -m scripts.seed

Passwords come from SEED_ADMIN_PASSWORD / SEED_EMPLOYEE_PASSWORD; development defaults are used
otherwise and printed once. Change them (Admin page) before anyone else can reach the server.
"""
import os

from sqlalchemy import select

from app.auth import hash_password
from app.db import Client, Profile, Session, User, init_db
from app.profiles import ATC_PROFILE, GENERIC_PROFILE

USERS = [
    ("admin@trafficomm.local", "Workspace Admin", "admin", os.environ.get("SEED_ADMIN_PASSWORD") or "admin-dev-password"),
    ("employee@trafficomm.local", "Sara (Employee)", "employee", os.environ.get("SEED_EMPLOYEE_PASSWORD") or "employee-dev-password"),
    ("employee2@trafficomm.local", "Omar (Employee)", "employee", os.environ.get("SEED_EMPLOYEE2_PASSWORD") or "employee2-dev-password"),
]


def main():
    init_db()
    with Session() as s:
        created = []
        for email, name, role, pw in USERS:
            if not s.scalar(select(User).where(User.email == email)):
                s.add(User(email=email, name=name, role=role, password_hash=hash_password(pw)))
                created.append((email, pw, role))
        atc = s.scalar(select(Client).where(Client.name == "Around The Clock Media"))
        if not atc:
            atc = Client(name="Around The Clock Media", short="ATC")
            s.add(atc)
            s.flush()
        if not s.scalar(select(Profile).where(Profile.client_id == atc.id)):
            s.add(Profile(client_id=atc.id, name="ATC – Brighton College / Bloom (Meta)", settings=ATC_PROFILE))
        gen = s.scalar(select(Client).where(Client.name == "Other client (generic)"))
        if not gen:
            gen = Client(name="Other client (generic)", short="GEN")
            s.add(gen)
            s.flush()
        if not s.scalar(select(Profile).where(Profile.client_id == gen.id)):
            s.add(Profile(client_id=gen.id, name="Generic multi-platform", settings=GENERIC_PROFILE))
        s.commit()
    for email, pw, role in created:
        print(f"created {role:8s} {email}  password: {pw}")
    print("seed complete")


if __name__ == "__main__":
    main()
