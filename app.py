from flask import (
    Flask, render_template, request,
    redirect, url_for, session, flash
)

import sqlite3
import uuid
from pathlib import Path
from decimal import Decimal, InvalidOperation
from werkzeug.security import (
    generate_password_hash,
    check_password_hash
)

app = Flask(__name__)

app.secret_key = "balancevault-development-secret"

DATABASE = Path(__file__).with_name("database.db")


# DATABASE CONNECTION

def get_db():
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


# INITIALIZE DATABASE

def initialize_database():

    connection = get_db()

    connection.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER UNIQUE NOT NULL,
        balance INTEGER NOT NULL DEFAULT 0 CHECK(balance >= 0),
        FOREIGN KEY(user_id) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS transfers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        transaction_id TEXT UNIQUE NOT NULL,
        sender_id INTEGER NOT NULL,
        receiver_id INTEGER NOT NULL,
        amount INTEGER NOT NULL CHECK(amount > 0),
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(sender_id) REFERENCES accounts(id),
        FOREIGN KEY(receiver_id) REFERENCES accounts(id),
        CHECK(sender_id != receiver_id)
    );

    CREATE TABLE IF NOT EXISTS ledger_entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        transaction_id TEXT NOT NULL,
        account_id INTEGER NOT NULL,
        entry_type TEXT NOT NULL
            CHECK(entry_type IN ('DEBIT', 'CREDIT')),
        amount INTEGER NOT NULL CHECK(amount > 0),
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(transaction_id)
            REFERENCES transfers(transaction_id),
        FOREIGN KEY(account_id)
            REFERENCES accounts(id)
    );
    """)

    connection.commit()
    connection.close()


# HOME PAGE

@app.route("/")
def home():

    if "user_id" in session:
        return redirect(url_for("dashboard"))

    return redirect(url_for("register"))


# USER REGISTRATION

@app.route("/register", methods=["GET", "POST"])
def register():

    if request.method == "POST":

        username = request.form["username"].strip()
        password = request.form["password"]

        if len(username) < 3:
            flash("Username must have at least 3 characters.")
            return redirect(url_for("register"))

        if len(password) < 8:
            flash("Password must have at least 8 characters.")
            return redirect(url_for("register"))

        password_hash = generate_password_hash(password)

        connection = get_db()

        try:

            cursor = connection.execute(
                """
                INSERT INTO users (username, password_hash)
                VALUES (?, ?)
                """,
                (username, password_hash)
            )

            user_id = cursor.lastrowid

            connection.execute(
                """
                INSERT INTO accounts (user_id, balance)
                VALUES (?, 0)
                """,
                (user_id,)
            )

            connection.commit()

            flash("Registration successful! Please log in.")
            return redirect(url_for("login"))

        except sqlite3.IntegrityError:

            connection.rollback()
            flash("Username already exists.")

        finally:
            connection.close()

    return render_template("register.html")


# USER LOGIN

@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "POST":

        username = request.form["username"].strip()
        password = request.form["password"]

        connection = get_db()

        user = connection.execute(
            """
            SELECT * FROM users
            WHERE username = ?
            """,
            (username,)
        ).fetchone()

        connection.close()

        if user and check_password_hash(
            user["password_hash"], password
        ):

            session.clear()

            session["user_id"] = user["id"]
            session["username"] = user["username"]

            return redirect(url_for("dashboard"))

        flash("Invalid username or password.")

    return render_template("login.html")


# USER DASHBOARD

@app.route("/dashboard")
def dashboard():

    if "user_id" not in session:
        return redirect(url_for("login"))

    connection = get_db()

    account = connection.execute(
        """
        SELECT id, balance
        FROM accounts
        WHERE user_id = ?
        """,
        (session["user_id"],)
    ).fetchone()

    if account is None:
        connection.close()
        return "Account not found", 404

    history = connection.execute(
        """
        SELECT
            le.transaction_id,
            le.entry_type,
            le.amount,
            le.created_at,
            CASE
                WHEN le.entry_type = 'DEBIT'
                THEN receiver.username
                ELSE sender.username
            END AS counterparty

        FROM ledger_entries le

        JOIN transfers t
            ON t.transaction_id = le.transaction_id

        JOIN accounts sender_account
            ON sender_account.id = t.sender_id

        JOIN users sender
            ON sender.id = sender_account.user_id

        JOIN accounts receiver_account
            ON receiver_account.id = t.receiver_id

        JOIN users receiver
            ON receiver.id = receiver_account.user_id

        WHERE le.account_id = ?

        ORDER BY le.created_at DESC, le.id DESC
        """,
        (account["id"],)
    ).fetchall()

    connection.close()

    balance_rupees = account["balance"] / 100

    return render_template(
        "dashboard.html",
        username=session["username"],
        balance=balance_rupees,
        history=history
    )


# MONEY TRANSFER

@app.route("/transfer", methods=["POST"])
def transfer():

    if "user_id" not in session:
        return redirect(url_for("login"))

    receiver_username = request.form["receiver"].strip()
    amount_input = request.form["amount"]

    try:
        amount_rupees = Decimal(amount_input)

        if not amount_rupees.is_finite() or amount_rupees <= 0:
            raise ValueError

        amount_paise_decimal = amount_rupees * 100

        if amount_paise_decimal != amount_paise_decimal.to_integral_value():
            raise ValueError

        amount_paise = int(amount_paise_decimal)

    except (InvalidOperation, ValueError):
        flash("Please enter a valid amount.")
        return redirect(url_for("dashboard"))

    connection = get_db()

    try:

        connection.execute("BEGIN IMMEDIATE")

        sender = connection.execute(
            """
            SELECT id, balance
            FROM accounts
            WHERE user_id = ?
            """,
            (session["user_id"],)
        ).fetchone()

        receiver = connection.execute(
            """
            SELECT accounts.id
            FROM accounts
            JOIN users
                ON users.id = accounts.user_id
            WHERE users.username = ?
            """,
            (receiver_username,)
        ).fetchone()

        if sender is None or receiver is None:
            raise ValueError("Sender or receiver account not found.")

        if sender["id"] == receiver["id"]:
            raise ValueError("You cannot transfer money to yourself.")

        if sender["balance"] < amount_paise:
            raise ValueError("Insufficient account balance.")

        transaction_id = str(uuid.uuid4())

        connection.execute(
            """
            UPDATE accounts
            SET balance = balance - ?
            WHERE id = ?
            """,
            (amount_paise, sender["id"])
        )

        connection.execute(
            """
            UPDATE accounts
            SET balance = balance + ?
            WHERE id = ?
            """,
            (amount_paise, receiver["id"])
        )

        connection.execute(
            """
            INSERT INTO transfers
            (transaction_id, sender_id, receiver_id, amount)
            VALUES (?, ?, ?, ?)
            """,
            (
                transaction_id,
                sender["id"],
                receiver["id"],
                amount_paise
            )
        )

        connection.execute(
            """
            INSERT INTO ledger_entries
            (transaction_id, account_id, entry_type, amount)
            VALUES (?, ?, 'DEBIT', ?)
            """,
            (transaction_id, sender["id"], amount_paise)
        )

        connection.execute(
            """
            INSERT INTO ledger_entries
            (transaction_id, account_id, entry_type, amount)
            VALUES (?, ?, 'CREDIT', ?)
            """,
            (transaction_id, receiver["id"], amount_paise)
        )

        connection.commit()

        flash("Money transferred successfully!")

    except ValueError as error:

        connection.rollback()
        flash(str(error))

    except sqlite3.Error:

        connection.rollback()
        flash("Transfer failed. Please try again.")

    finally:
        connection.close()

    return redirect(url_for("dashboard"))


# USER LOGOUT
# DEMO DEPOSIT

@app.route("/deposit", methods=["POST"])
def deposit():

    if "user_id" not in session:
        return redirect(url_for("login"))

    try:
        amount = Decimal(request.form["amount"])

        if not amount.is_finite() or amount <= 0:
            raise ValueError

        paise = amount * 100

        if paise != paise.to_integral_value():
            raise ValueError

        paise = int(paise)

        connection = get_db()

        connection.execute(
            """
            UPDATE accounts
            SET balance = balance + ?
            WHERE user_id = ?
            """,
            (paise, session["user_id"])
        )

        connection.commit()
        connection.close()

        flash("Demo money added successfully!")

    except (ValueError, InvalidOperation):
        flash("Enter a valid deposit amount.")

    return redirect(url_for("dashboard"))
@app.route("/logout")
def logout():

    session.clear()

    return redirect(url_for("login"))


# RUN APPLICATION

if __name__ == "__main__":

    initialize_database()

    app.run(debug=True)