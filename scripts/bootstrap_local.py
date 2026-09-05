"""Create local infrastructure secrets through the installed platform command."""

from adaptive_trader.platform.cli import app


def main() -> None:
    """Run the fixed local secret bootstrap command."""

    app(prog_name="bootstrap_local.py", args=["secrets", "bootstrap-local"])


if __name__ == "__main__":
    main()
