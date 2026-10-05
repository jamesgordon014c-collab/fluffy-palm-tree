import csv


def main():
    with open("contacts.csv", newline="") as f:
        for row in csv.DictReader(f):
            print(f"Name: {row['name']} | Email: {row['email']} | Company: {row['company']}")


if __name__ == "__main__":
    main()
