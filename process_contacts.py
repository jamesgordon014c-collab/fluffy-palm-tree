import csv


def main():
    with open("contacts.csv", newline="") as f:
        for row in csv.DictReader(f):
            # The company column has been called both "company" and "organization".
            company = row.get("company") or row.get("organization", "")
            print(f"Name: {row['name']} | Email: {row['email']} | Company: {company}")


if __name__ == "__main__":
    main()
