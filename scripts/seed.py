"""Create the initial clients and client profiles (idempotent).

    python -m scripts.seed

No user accounts or passwords are created: everyone signs in with Zoho. The first administrator
comes from ZOHO_ADMIN_EMAILS on their first Zoho sign-in, and admins grant everyone else access
on the Admin page.
"""
from sqlalchemy import select

from app.db import Client, Profile, Session, init_db
from app.profiles import ATC_PROFILE, GENERIC_PROFILE


def main():
    init_db()
    with Session() as s:
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
    print("seed complete")


if __name__ == "__main__":
    main()
