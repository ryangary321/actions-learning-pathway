# Send an SMS with the Vonage Python SDK

The Vonage Python SDK lets you send SMS messages with just a few lines of code. Install the SDK first:

```bash
pip install vonage
```

Use your API credentials to send a message. Replace the placeholders with your own values or load them from environment variables to keep secrets out of source control.

```python
import os
import vonage

client = vonage.Client(
    api_key=os.environ.get("VONAGE_API_KEY", "your_api_key"),
    api_secret=os.environ.get("VONAGE_API_SECRET", "your_api_secret"),
)
sms = vonage.Sms(client)

response_data = sms.send_message(
    {
        "from": "Vonage APIs",
        "to": "13854990844",
        "text": "A text message sent using the Vonage SMS API",
    }
)

if response_data["messages"][0]["status"] == "0":
    print("Message sent successfully.")
else:
    print(f"Message failed with error: {response_data['messages'][0]['error-text']}")
```

The SDK returns a list of message objects; a status of `"0"` means the SMS was accepted. Review the returned fields to log the message ID or report errors to monitoring tools.
