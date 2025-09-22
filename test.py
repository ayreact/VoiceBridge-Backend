from spitch import Spitch

client = Spitch(api_key="sk_nuIVeB9aNOb4ba7mtE6zjVWA9vyrZjfmdzxk45YJ")

try:
    response = client.speech.transcribe(
        language="en",
        content=b"hello world"
    )
    print(response.text)
except Exception as e:
    print("SDK Call Failed:", e)
