from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI()

@app.get("/")
def dashboard():
    return HTMLResponse("""
        <h1>DealBite is Live 🚀</h1>
        <p>This is your deployed MVP foundation.</p>
    """)
