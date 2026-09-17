"""One-off script: python seed_child.py <name> <4-digit-pin>"""

import sys

from app.database import Base, SessionLocal, engine
from app.models import Child
from app.security import hash_pin

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python seed_child.py <name> <4-digit-pin>")
        sys.exit(1)

    name, pin = sys.argv[1], sys.argv[2]
    if not (pin.isdigit() and len(pin) == 4):
        print("PIN must be exactly 4 digits")
        sys.exit(1)

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    child = Child(name=name, pin_hash=hash_pin(pin))
    db.add(child)
    db.commit()
    print(f"Created child id={child.id} name={child.name}")
