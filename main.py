import os
import base64 
import requests 
import stripe 
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, UploadFile, File, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel
from openai import OpenAI
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "sk_test_mock_key")  # Valetunniste testausta varten

limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="Asuntoesite AI - Pro SaaS Edition", version="3.0")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def hae_paikkatieto_ja_palvelut(osoite):
    info = []
    try:
        url = f"https://api.digitransit.fi/geocoding/v1/search?text={osoite}&size=1"
        headers = {"User-Agent": "AsuntoSaas/3.0"}
        vastaus = requests.get(url, headers=headers, timeout=5)

        if vastaus.status_code == 200 and vastaus.json()["features"]:
            props = vastaus.json()["features"][0]["properties"]
            alue = props.get("localadmin", "alueella")
            info.append(f"Sijainti vahvistettu alueella {alue}. Loistavat liikenneyhteydet.")
    except Exception:
        info.append("Aluetiedot tarkistettu.")
    return " ".join(info)

class AsuntoPyynto(BaseModel):
    osoite: str
    pinta_ala: str
    huoneet: str
    hoitovastike: str = ""
    energialuokka: str = ""
    tontti: str = "Oma"
    savy: str = "Ammattimainen ja myyvä"
    kieli: str = "Suomi"
    lisatiedot: str = ""

# --- MAKSUTAPAHTUMA (STRIPE / TESTITILA) ---
@app.post("/luo-maksusessio")
def luo_maksusessio():
    # Jos sinulla ei ole vielä Stripe-avainta, toimitaan testitilassa!
    if os.getenv("STRIPE_SECRET_KEY") is None:
        return {"status": "test_mode", "message": "Testimaksu hyväksytty! Voit nyt käyttää Pro-ominaisuuksia."}
    try:
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': 'eur',
                    'product_data': {'name': 'Asuntoesite AI pro - Kuukausitilaus'},
                    'unit_amount': 2900,  # 29.00 EUR
                    'recurring': {'interval': 'month'},
                },
                'quantity': 1,
            }],
            mode='subscription',
            success_url='http://127.0.0.1:8000/?maksu=onnistui',
            cancel_url='http://127.0.0.1:8000/?maksu=peruttu',
        )
        return {"status": "redirect", "url": checkout_session.url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --- TEKOÄLYKUTSU ---
@app.post("/luo-esite")
@limiter.limit("10/minute")
def luo_esite_api(request: Request, data: AsuntoPyynto):
    try:
        alue_info = hae_paikkatieto_ja_palvelut(data.osoite)

        system_instruction = f"""
Olet Suomen kiinteistönvälityslainsäädäntöön ja KVKL:n Hyvään välitystapaan erikoistunut asiantuntija-AI. 

TÄRKEIMMÄT SÄÄNNÖT JA HALLUSINAATION ESTO:
1. STRICT FACTUALITY (Täysi tosiasiallisuus): Käytä AINOASTAAN käyttäjän syöttämiä faktoja ja automaattisesti haettuja aluetietoja. 
2. ÄLÄ KOSKAAN KEKSI OMIEN OLETTAMUSTEN PERUSTEELLA tietoja asunnosta kunnosta, remonttihistoriasta, neliöistä, kerroksesta, parvekkeesta, hissistä tai taloyhtiön päätöksistä. 
3. JOS JOKIN PAKOLLINEN TIETO PUUTTUU (kuten energiatodistus, hoitovastike tai tontin omistus, merkitse se selkeästi tekstiin muodossa: "[Tieto puuttuu / Tarkistettava isännöitsijäntodistuksesta]". Älä jätä asiaa mainitsematta äläkä keksi arviota. 
4. HYVÄ VÄLITYSTAPA:
- Esitä asunnon tiedot rehellisesti ja harhaanjohtamatta. 
- Älä käytä harhaanjohtavia yleistyksiä (esim. "uudenveroinen" tai "täysin remontoitu"), ellei lisätiedoissa niin erikseen mainita. 
- Varmista, että energiatodistus/energialuokka, tontin omistusmuoto ja hoitovastike ovat selkeästi esillä. 
5. KIELI ja SÄVY:
- Kieli: {data.kieli}
- Sävy: {data.savy} (Pidä sävy silti asiallisena ja luotettavana). 
6. RAKENNE:
- Houkutteleva otsikko
- Myyvä, mutta täysin tosipohjainen leipäteksti
- Kohde- ja sijaintitiedot selkeänä listauksena
- Toimintakehotus (esim. "Ota yhteyttä ja sovi yksityisesittely!") 
""" 

        prompti = f"""
Luo myyntiesite seuraavilla tiedoilla: 
- Osoite: {data.osoite}
- Pinta-ala: {data.pinta_ala}
- Kokoonpano: {data.huoneet}
- Hoitovastike: {data.hoitovastike if data.hoitovastike else 'Ei ilmoitettu'}
- Energialuokka: {data.energialuokka if data.energialuokka else 'Ei ilmoitettu'}
- Tontti: {data.tontti}
- Automaattiset sijaintitiedot: {alue_info}
- Lisätiedot: {data.lisatiedot}
""" 

        vastaus = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": prompti}
            ],
            temperature=0.6
        )

        esite_teksti = vastaus.choices[0].message.content 

        with open("viimeisin_esite.txt", "w", encoding="utf-8") as f:
            f.write(f"Osoite: {data.osoite}\n\n{esite_teksti}")
        return {"status": "success", "esite": esite_teksti}

    except Exception as e: 
        raise HTTPException(status_code=500, detail=str(e))

# --- VISION AI ---
@app.post("/analysoi-kuva")
@limiter.limit("5/minute")
async def analysoi_kuva(request: Request, file: UploadFile = File(...)):
    try:
        contents = await file.read()
        base64_image = base64.b64encode(contents).decode('utf-8')

        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Kuvaile tämä asuntokuva tiiviisti myyntiesitettä varten suomeksi. Nimeä huone, materiaalit, valoisa ilme ja näkyvät edut"},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                    ]
                }
            ],
            max_tokens=200
        )
        return {"kuvaus": response.choices[0].message.content}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --- PDF GENERATOR ---
@app.get("/lataa-pdf")
def lataa_pdf():
    pdf_filename = "Asuntoesite.pdf"
    if not os.path.exists("viimeisin_esite.txt"):
        raise HTTPException(status_code=400, detail="Esitettä ei ole vielä luotu.")
    with open("viimeisin_esite.txt", "r", encoding="utf-8") as f:
        sisalto = f.read()
    doc = SimpleDocTemplate(pdf_filename, pagesize=A4, rightMargin=40, leftMargin=40, topMargin=40, bottomMargin=40)
    styles = getSampleStyleSheet()
    style_body = ParagraphStyle('Body', parent=styles['normal'], fontSize=11, leading=16)

    story = []
    for kappale in sisalto.split('\n\n'):
        if kappale.strip():
            muotoiltu = kappale.replace('\n', '<br/>')
            story.append(Paragraph(muotoiltu, style_body))
            story.append(Spacer(1, 12))
    doc.build(story)
    return FileResponse(pdf_filename, media_type='application/pdf', filename=pdf_filename)

@app.get("/", response_class=HTMLResponse)
def luo_kotisivu():
    with open("index.html", "r", encoding="utf-8") as f:
        return f.read()


            
    