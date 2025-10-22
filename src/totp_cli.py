import pyotp
import sys

# Get the secret from command-line arguments
if len(sys.argv) < 2:
    print("Error: No secret provided.")
    sys.exit(1)

secret = sys.argv[1]

# Generate the current TOTP
totp = pyotp.TOTP(secret)
print(totp.now())


