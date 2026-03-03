import argparse
import csv
import io
import json
import math
import os
import re
import shutil
import sys
import time
from datetime import datetime
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.common.exceptions import ElementClickInterceptedException, NoSuchElementException, TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait

try:
    import pytesseract
    from PIL import Image
except Exception:
    pytesseract = None
    Image = None

# Comandos para ejecutar:
# MODO NORMAL: python scraper.py "https://icepar.specparts.shop/" --visible
# MODO RESUME (continuar): python scraper.py "https://icepar.specparts.shop/" --visible --resume
# MODO DESARROLLADOR (exploración limitada): python scraper.py "https://icepar.specparts.shop/" --visible --dev
# MODO ESPECÍFICO (selección por índices): python scraper.py "https://icepar.specparts.shop/" --visible --specific "1,1,1,1,1"

LOGIN_URL = "https://icepar.specparts.shop/"
EMAIL = "ecolarusso@icepar-sa.com.ar"
PASSWORD = "piero25*"

EMAIL_XPATH = "/html/body/section/div/div/div[1]/div/form/div[1]/input"
PASSWORD_XPATH = "/html/body/section/div/div/div[1]/div/form/div[2]/div/input"
SUBMIT_XPATH = "/html/body/section/div/div/div[1]/div/form/div[3]/div[2]/button"
VEHICULO_TAB_XPATH = "/html/body/section[1]/div/div[2]/div/ul/li[3]/a"
SEARCH_BUTTON_XPATH = "/html/body/section[1]/div/div[2]/div/div/div[3]/div/form/div[2]/div/div/button"
SELECT_XPATHS = [
    "/html/body/section[1]/div/div[2]/div/div/div[3]/div/form/div[1]/div/div[1]/div/select",
    "/html/body/section[1]/div/div[2]/div/div/div[3]/div/form/div[1]/div/div[2]/div/select",
    "/html/body/section[1]/div/div[2]/div/div/div[3]/div/form/div[1]/div/div[3]/div/select",
    "/html/body/section[1]/div/div[2]/div/div/div[3]/div/form/div[1]/div/div[4]/div/select",
    "/html/body/section[1]/div/div[2]/div/div/div[3]/div/form/div[1]/div/div[5]/div/select",
]
RESULTS_CONTAINER_XPATH = "/html/body/section/div/div/div[2]/div/div/div/div[2]/div[2]/div"
RESULT_CARD_LINK_XPATH_TEMPLATE = "/html/body/section/div/div/div[2]/div/div/div/div[2]/div[2]/div/div[{i}]/div[1]/div/div[1]/a"
RESULT_CARD_CODE_XPATH_TEMPLATE = "/html/body/section/div/div/div[2]/div/div/div/div[2]/div[2]/div/div[{i}]/div[1]/div/div[2]/div[1]/a/h4"
DETAIL_IMAGES_CONTAINER_XPATH = "/html/body/section[2]/div/div/div[1]/div/div[1]/div/div"
PRODUCTS_PATH = "/products"
PRODUCTS_QUERY_KEYS = [
    "vehicle[grouped_segment]",
    "vehicle[brand]",
    "vehicle[master_model]",
    "vehicle[version]",
    "vehicle[sold_from_year]",
]

_DEVNULL_STREAM = None
_OCR_READY = False
HTTP_MAX_RETRIES = 1
HTTP_RETRY_DELAY_SECONDS = 1.5
RESULTADOS_FILENAME = "resultados.csv"


def _dedupe_keep_order(values: list[str]) -> list[str]:
    seen = set()
    output = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        output.append(value)
    return output


def _normalize_code(value: str) -> str:
    return " ".join((value or "").strip().split())


def _parse_codes_cell(value: str) -> list[str]:
    if not value:
        return []
    parsed = []
    seen = set()
    for part in value.split(","):
        code = _normalize_code(part)
        if not code or code in seen:
            continue
        seen.add(code)
        parsed.append(code)
    return parsed


def _join_codes_cell(codes: list[str]) -> str:
    return ", ".join(codes)


def build_http_session_from_driver(driver: webdriver.Chrome) -> requests.Session:
    session = requests.Session()
    parsed_login = urlparse(LOGIN_URL)
    default_host = parsed_login.hostname

    for cookie in driver.get_cookies():
        name = cookie.get("name")
        value = cookie.get("value")
        if not name or value is None:
            continue
        domain = cookie.get("domain") or default_host
        path = cookie.get("path") or "/"
        session.cookies.set(name, value, domain=domain, path=path)

    try:
        user_agent = driver.execute_script("return navigator.userAgent;")
    except Exception:
        user_agent = "Mozilla/5.0"

    session.headers.update({
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": LOGIN_URL,
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    })
    return session


def build_products_params(filters: list[str], page: int | None = None) -> dict[str, str]:
    params: dict[str, str] = {
        "vehicle_id": "",
    }
    for index, key in enumerate(PRODUCTS_QUERY_KEYS):
        params[key] = filters[index] if index < len(filters) else ""
    if page is not None and page > 1:
        params["page"] = str(page)
    return params


def build_products_url(filters: list[str], page: int | None = None) -> str:
    base_products_url = urljoin(LOGIN_URL, PRODUCTS_PATH)
    params = build_products_params(filters, page=page)
    return f"{base_products_url}?{urlencode(params)}"


def extract_codes_from_products_html(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    candidates = soup.select("a h4")
    codes = []
    for node in candidates:
        text = node.get_text(strip=True)
        if not text or not re.search(r"\d", text):
            continue
        codes.append(text)
    return _dedupe_keep_order(codes)


def extract_product_links_from_products_html(html: str, current_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for node in soup.select("a[href]"):
        href = (node.get("href") or "").strip()
        if not href:
            continue
        full_url = urljoin(current_url, href)
        if "/products/" not in full_url:
            continue
        links.append(full_url)
    return _dedupe_keep_order(links)


def extract_products_from_products_html(html: str, current_url: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    products = []
    seen_codes = set()

    for node in soup.select("a h4"):
        code = _normalize_code(node.get_text(strip=True))
        if not code or not re.search(r"\d", code):
            continue

        anchor = node.find_parent("a")
        if anchor is None:
            continue
        href = (anchor.get("href") or "").strip()
        if not href:
            continue

        detail_url = urljoin(current_url, href)
        if "/products/detail" not in detail_url:
            continue
        if code in seen_codes:
            continue

        seen_codes.add(code)
        products.append({"code": code, "detail_url": detail_url})

    return products


def extract_total_items_from_products_html(html: str) -> int:
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    match = re.search(r"de\s+(\d+)\s+resultados", text, flags=re.IGNORECASE)
    if not match:
        return 0
    return int(match.group(1))


def extract_next_page_url_from_html(html: str, current_url: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")

    next_candidates = [
        "a[rel='next']",
        "li.next a",
        "a.page-link[rel='next']",
        "a[aria-label='Next']",
        "a[aria-label='Siguiente']",
    ]
    for selector in next_candidates:
        node = soup.select_one(selector)
        if node and node.get("href"):
            return urljoin(current_url, node["href"])

    parsed = urlparse(current_url)
    query_params = parse_qs(parsed.query)
    current_page = 1
    if "page" in query_params:
        try:
            current_page = int(query_params["page"][0])
        except Exception:
            current_page = 1

    all_pages = []
    for node in soup.select("a.page-link[href], ul.pagination a[href], nav a[href]"):
        href = node.get("href")
        if not href:
            continue
        parsed_href = urlparse(urljoin(current_url, href))
        href_params = parse_qs(parsed_href.query)
        if "page" not in href_params:
            continue
        try:
            page_num = int(href_params["page"][0])
            all_pages.append((page_num, urljoin(current_url, href)))
        except Exception:
            continue

    next_pages = [item for item in all_pages if item[0] == current_page + 1]
    if next_pages:
        return next_pages[0][1]

    return None


def extract_results_via_http(driver: webdriver.Chrome, filters: list[str], timeout: int) -> tuple[list[str], list[dict[str, str]], str | None, str | None]:
    if not filters:
        return [], [], None, "No hay filtros para construir la consulta"

    last_error = None
    last_url = None
    attempts = HTTP_MAX_RETRIES + 1

    for attempt in range(1, attempts + 1):
        session = build_http_session_from_driver(driver)
        first_url = build_products_url(filters)

        try:
            response = session.get(first_url, timeout=timeout)
        except Exception as e:
            last_error = f"Error HTTP inicial: {e}"
            if attempt < attempts:
                print(f"⚠️ Intento HTTP {attempt}/{attempts} fallido: {last_error}. Reintentando...")
                time.sleep(HTTP_RETRY_DELAY_SECONDS)
                continue
            return [], [], None, f"{last_error} (agotados {attempts} intentos)"

        if response.status_code != 200:
            last_url = response.url
            last_error = f"HTTP {response.status_code} en consulta inicial"
            if attempt < attempts:
                print(f"⚠️ Intento HTTP {attempt}/{attempts} fallido: {last_error}. Reintentando...")
                time.sleep(HTTP_RETRY_DELAY_SECONDS)
                continue
            return [], [], last_url, f"{last_error} (agotados {attempts} intentos)"

        combined_products = extract_products_from_products_html(response.text, response.url)
        total_items = extract_total_items_from_products_html(response.text)
        final_url = response.url

        visited_urls = {response.url}
        next_url = extract_next_page_url_from_html(response.text, response.url)
        page_error = None

        while next_url and next_url not in visited_urls:
            try:
                page_response = session.get(next_url, timeout=timeout)
            except Exception as e:
                page_error = f"Error HTTP en paginación: {e}"
                break

            if page_response.status_code != 200:
                page_error = f"HTTP {page_response.status_code} en paginación"
                break

            visited_urls.add(page_response.url)
            final_url = page_response.url
            page_products = extract_products_from_products_html(page_response.text, page_response.url)
            combined_products.extend(page_products)
            next_url = extract_next_page_url_from_html(page_response.text, page_response.url)

        if page_error:
            last_error = page_error
            last_url = final_url
            if attempt < attempts:
                print(f"⚠️ Intento HTTP {attempt}/{attempts} fallido: {last_error}. Reintentando...")
                time.sleep(HTTP_RETRY_DELAY_SECONDS)
                continue
            unique_products = []
            seen_codes = set()
            for product in combined_products:
                code = _normalize_code(product.get("code", ""))
                if not code or code in seen_codes:
                    continue
                seen_codes.add(code)
                unique_products.append(product)
            return [product["code"] for product in unique_products], unique_products, final_url, f"{last_error} (agotados {attempts} intentos)"

        unique_products = []
        seen_codes = set()
        for product in combined_products:
            code = _normalize_code(product.get("code", ""))
            if not code or code in seen_codes:
                continue
            seen_codes.add(code)
            unique_products.append({"code": code, "detail_url": product.get("detail_url", "")})

        unique_codes = [product["code"] for product in unique_products]
        if total_items and len(unique_codes) < min(total_items, 24):
            last_error = "Extracción parcial detectada: revisar selectores HTML"
            last_url = final_url
            if attempt < attempts:
                print(f"⚠️ Intento HTTP {attempt}/{attempts} con extracción parcial. Reintentando...")
                time.sleep(HTTP_RETRY_DELAY_SECONDS)
                continue
            return unique_codes, unique_products, final_url, f"{last_error} (agotados {attempts} intentos)"

        return unique_codes, unique_products, final_url, None

    return [], [], last_url, last_error or "Error HTTP desconocido"


def ensure_resultados_csv_header(filename: str = RESULTADOS_FILENAME) -> None:
    expected_header = ["aplicación", "código"]

    if not os.path.exists(filename):
        with open(filename, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(expected_header)
        return

    with open(filename, "r", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        first_row = next(reader, None)

    if first_row == expected_header:
        return

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_name = f"resultados_legacy_{timestamp}.csv"
    shutil.move(filename, backup_name)

    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(expected_header)

    print(f"🗂️ Formato CSV legado detectado. Backup generado: {backup_name}")


def load_registered_codes(filename: str = RESULTADOS_FILENAME) -> set[str]:
    registered_codes = set()
    if not os.path.exists(filename):
        return registered_codes

    try:
        with open(filename, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames or []

            if "código" in fieldnames:
                for row in reader:
                    for code in _parse_codes_cell(row.get("código", "")):
                        registered_codes.add(code)
                return registered_codes

            if "Códigos" in fieldnames:
                for row in reader:
                    for code in _parse_codes_cell(row.get("Códigos", "")):
                        registered_codes.add(code)
                return registered_codes
    except Exception as e:
        print(f"⚠️ Error leyendo códigos registrados: {e}")

    return registered_codes


def append_ocr_rows(rows: list[dict[str, str]], filename: str = RESULTADOS_FILENAME) -> None:
    if not rows:
        return

    ensure_resultados_csv_header(filename)
    with open(filename, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["aplicación", "código"])
        writer.writerows(rows)


def load_application_codes_map(filename: str = RESULTADOS_FILENAME) -> dict[str, list[str]]:
    application_codes: dict[str, list[str]] = {}
    if not os.path.exists(filename):
        return application_codes

    try:
        with open(filename, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames or []
            if "aplicación" in fieldnames and "código" in fieldnames:
                for row in reader:
                    application = " ".join((row.get("aplicación", "") or "").split()).strip()
                    if not application:
                        continue
                    codes = _parse_codes_cell(row.get("código", ""))
                    if not codes:
                        continue
                    if application not in application_codes:
                        application_codes[application] = []
                    for code in codes:
                        if code not in application_codes[application]:
                            application_codes[application].append(code)
                return application_codes

            if "Segmento" in fieldnames and "Códigos" in fieldnames:
                for row in reader:
                    application = " ".join((row.get("Aplicación", "") or "").split()).strip()
                    if not application:
                        continue
                    codes = _parse_codes_cell(row.get("Códigos", ""))
                    if not codes:
                        continue
                    if application not in application_codes:
                        application_codes[application] = []
                    for code in codes:
                        if code not in application_codes[application]:
                            application_codes[application].append(code)
                return application_codes
    except Exception as e:
        print(f"⚠️ Error leyendo aplicaciones/códigos: {e}")

    return application_codes


def save_application_codes_map(application_codes: dict[str, list[str]], filename: str = RESULTADOS_FILENAME) -> None:
    ensure_resultados_csv_header(filename)
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["aplicación", "código"])
        writer.writeheader()
        for application, codes in application_codes.items():
            writer.writerow({
                "aplicación": application,
                "código": _join_codes_cell(codes),
            })


def enable_silent_mode() -> None:
    global _DEVNULL_STREAM
    if _DEVNULL_STREAM is None:
        _DEVNULL_STREAM = open(os.devnull, "w", encoding="utf-8")
    sys.stdout = _DEVNULL_STREAM
    sys.stderr = _DEVNULL_STREAM


def get_last_processed_filters(filename: str = "resultados.csv") -> list[str] | None:
    try:
        with open(filename, "r", encoding="utf-8") as f:
            lines = [row for row in csv.reader(f) if any(row)]
            if len(lines) < 2:
                return None
            header = [col.strip() for col in lines[0]]
            expected_legacy = ["Segmento", "Marca", "Modelo", "Versión", "Año"]
            if header[:5] != expected_legacy:
                return None
            last_row = lines[-1]
            return [val.strip() for val in last_row[:5] if val.strip()]
    except FileNotFoundError:
        return None
    except Exception as e:
        print(f"⚠️ Error leyendo archivo para reanudar: {e}")
        return None


def setup_ocr() -> bool:
    global _OCR_READY
    if pytesseract is None or Image is None:
        print("⚠️ OCR no disponible: faltan dependencias de Python (pytesseract/Pillow).")
        _OCR_READY = False
        return False

    if shutil.which("tesseract") is None:
        print("⚠️ OCR no disponible: no se encontró Tesseract en PATH.")
        _OCR_READY = False
        return False

    _OCR_READY = True
    print("✅ OCR inicializado con Tesseract.")
    return True


def ocr_text_from_image_bytes(image_bytes: bytes) -> str:
    if not _OCR_READY:
        return ""

    try:
        image = Image.open(io.BytesIO(image_bytes))
        text = pytesseract.image_to_string(image)
        return " ".join(text.split())
    except Exception as e:
        print(f"    ⚠️ Error OCR en imagen: {e}")
        return ""


def ocr_text_from_image_element(image_element, http_session: requests.Session | None = None, timeout: int = 10) -> str:
    if not _OCR_READY:
        return ""

    image_url = ""
    try:
        image_url = (image_element.get_attribute("src") or image_element.get_attribute("data-src") or "").strip()
    except Exception:
        image_url = ""

    if http_session is not None and image_url and image_url.startswith("http"):
        try:
            response = http_session.get(image_url, timeout=timeout)
            if response.status_code == 200 and response.content:
                text = ocr_text_from_image_bytes(response.content)
                if text:
                    return text
        except Exception as e:
            print(f"    ⚠️ Error descargando imagen para OCR: {e}")

    try:
        png_bytes = image_element.screenshot_as_png
        return ocr_text_from_image_bytes(png_bytes)
    except Exception as e:
        print(f"    ⚠️ Error OCR en imagen: {e}")
        return ""


def extract_product_image_texts(driver: webdriver.Chrome, timeout: int, http_session: requests.Session | None = None, ocr_max_images: int = 30) -> list[str]:
    try:
        container = WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.XPATH, DETAIL_IMAGES_CONTAINER_XPATH))
        )
    except TimeoutException:
        print("    ⚠️ No se encontró contenedor de imágenes del detalle.")
        return []

    raw_images = container.find_elements(By.XPATH, ".//img")
    if not raw_images:
        print("    ⚠️ No hay imágenes en el detalle.")
        return []

    candidates = []
    seen_sources = set()
    for image in raw_images:
        try:
            src = (image.get_attribute("src") or image.get_attribute("data-src") or "").strip()
            if src and src in seen_sources:
                continue
            if src:
                seen_sources.add(src)
            if not image.is_displayed():
                continue
            candidates.append(image)
        except Exception:
            continue

    images = candidates if candidates else raw_images

    extracted_texts = []
    batch_size = max(1, ocr_max_images)
    total_images = len(images)
    for batch_start in range(0, total_images, batch_size):
        batch_end = min(batch_start + batch_size, total_images)
        print(f"    📦 Procesando lote de imágenes {batch_start + 1}-{batch_end} de {total_images}...")
        batch_images = images[batch_start:batch_end]
        for offset, image in enumerate(batch_images, start=1):
            img_index = batch_start + offset
            text = ocr_text_from_image_element(image, http_session=http_session, timeout=timeout)
            extracted_texts.append(text)
            if text:
                print(f"    🖼️ OCR imagen {img_index}/{total_images}: {text}")
            else:
                print(f"    🖼️ OCR imagen {img_index}/{total_images}: (sin texto)")

    return extracted_texts


def create_driver(headless: bool) -> webdriver.Chrome:
    options = Options()
    if headless:
        options.add_argument("--headless=new")
    
    # Optimizaciones y evasión básica de anti-bots
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-gpu")
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    
    return webdriver.Chrome(options=options)


def login(driver: webdriver.Chrome, timeout: int) -> bool:
    try:
        print(f"Abriendo {LOGIN_URL}...")
        driver.get(LOGIN_URL)

        submit_button = WebDriverWait(driver, 3).until(
            EC.presence_of_element_located((By.XPATH, SUBMIT_XPATH))
        )
        button_text = (submit_button.get_attribute("textContent") or submit_button.text or "").strip()

        if button_text.lower() != "iniciar sesión":
            print("Formulario de login no detectado. Continuando con sesión activa.")
            return True

        email_input = WebDriverWait(driver, timeout).until(
            EC.visibility_of_element_located((By.XPATH, EMAIL_XPATH))
        )
        email_input.clear()
        email_input.send_keys(EMAIL)

        password_input = driver.find_element(By.XPATH, PASSWORD_XPATH)
        password_input.clear()
        password_input.send_keys(PASSWORD)

        submit_button.click()

        WebDriverWait(driver, timeout).until(
            lambda d: d.current_url != LOGIN_URL
        )
        print("✅ Sesión iniciada correctamente.")
        return True

    except TimeoutException:
        print("Formulario de login no detectado. Continuando con sesión activa.")
        return True
    except NoSuchElementException as e:
        print(f"❌ Error al iniciar sesión: {str(e)}")
        return False
    except Exception as e:
        print(f"❌ Error al iniciar sesión: {str(e)}")
        return False


def click_element(driver: webdriver.Chrome, element) -> None:
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
    try:
        element.click()
    except ElementClickInterceptedException:
        driver.execute_script("arguments[0].click();", element)


def navigate_back_to_form(driver: webdriver.Chrome, timeout: int) -> None:
    driver.back()
    try:
        vehicle_tab = WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable((By.XPATH, VEHICULO_TAB_XPATH))
        )
        click_element(driver, vehicle_tab)
    except TimeoutException:
        driver.get(LOGIN_URL)
        try:
            vehicle_tab = WebDriverWait(driver, timeout).until(
                EC.element_to_be_clickable((By.XPATH, VEHICULO_TAB_XPATH))
            )
            click_element(driver, vehicle_tab)
        except Exception:
            pass


def ensure_vehicle_form_ready(driver: webdriver.Chrome, timeout: int) -> bool:
    short_timeout = min(timeout, 5)
    try:
        vehicle_tab = WebDriverWait(driver, short_timeout).until(
            EC.element_to_be_clickable((By.XPATH, VEHICULO_TAB_XPATH))
        )
        click_element(driver, vehicle_tab)
        WebDriverWait(driver, short_timeout).until(
            EC.presence_of_element_located((By.XPATH, SELECT_XPATHS[0]))
        )
        return True
    except Exception:
        try:
            driver.get(LOGIN_URL)
            vehicle_tab = WebDriverWait(driver, short_timeout).until(
                EC.element_to_be_clickable((By.XPATH, VEHICULO_TAB_XPATH))
            )
            click_element(driver, vehicle_tab)
            WebDriverWait(driver, short_timeout).until(
                EC.presence_of_element_located((By.XPATH, SELECT_XPATHS[0]))
            )
            return True
        except Exception:
            return False


def restore_select_path(driver: webdriver.Chrome, timeout: int, selected_indices: list[int]) -> bool:
    if not selected_indices:
        return True

    if not ensure_vehicle_form_ready(driver, timeout):
        return False

    restore_timeout = min(timeout, 4)

    for level, option_index in enumerate(selected_indices):
        xpath = SELECT_XPATHS[level]
        restored = False

        for _ in range(2):
            try:
                def select_ready(d, x=xpath, idx=option_index):
                    elem = d.find_element(By.XPATH, x)
                    options = Select(elem).options
                    return elem.is_enabled() and len(options) > idx

                WebDriverWait(driver, restore_timeout).until(select_ready)
                select_elem = driver.find_element(By.XPATH, xpath)
                Select(select_elem).select_by_index(option_index)
                time.sleep(0.5)
                restored = True
                break
            except Exception:
                if not ensure_vehicle_form_ready(driver, timeout):
                    break

        if not restored:
            print(f"⚠️ No se pudo restaurar nivel {level + 1}. Reintentando navegación.")
            return False

    return True


def run_ocr_for_products(driver: webdriver.Chrome, timeout: int, products: list[dict[str, str]], ocr_max_images: int, registered_codes: set[str], application_codes: dict[str, list[str]], csv_filename: str = RESULTADOS_FILENAME) -> None:
    http_session = build_http_session_from_driver(driver)
    total = len(products)
    for index, product in enumerate(products, start=1):
        code = _normalize_code(product.get("code", ""))
        product_url = (product.get("detail_url") or "").strip()

        if not code:
            print(f"  ⚠️ Producto OCR {index}/{total} sin código válido. Omitiendo.")
            continue

        if code in registered_codes:
            print(f"  ⏭️ Producto OCR {index}/{total}: código ya registrado ({code})")
            continue

        if not product_url:
            print(f"  ⚠️ Producto OCR {index}/{total} sin URL de detalle ({code}). Omitiendo.")
            registered_codes.add(code)
            continue

        try:
            print(f"  🔗 Producto OCR {index}/{total}: {product_url}")
            driver.get(product_url)
            detail_texts = extract_product_image_texts(driver, timeout=timeout, http_session=http_session, ocr_max_images=ocr_max_images)
            print(f"    📄 Textos OCR detectados: {len(detail_texts)}")

            applications_for_product = []
            seen_applications = set()
            for text in detail_texts:
                application = " ".join((text or "").split()).strip()
                if not application or application in seen_applications:
                    continue
                seen_applications.add(application)
                applications_for_product.append(application)

            for application in applications_for_product:
                if application not in application_codes:
                    application_codes[application] = []
                if code not in application_codes[application]:
                    application_codes[application].append(code)

            if applications_for_product:
                save_application_codes_map(application_codes, filename=csv_filename)
            registered_codes.add(code)
        except Exception as e:
            print(f"    ⚠️ Error procesando OCR del producto {index}: {e}")


def extract_visible_products(driver: webdriver.Chrome) -> list[str]:
    codes = []
    try:
        items = driver.find_elements(By.XPATH, f"{RESULTS_CONTAINER_XPATH}/div")
        count_visible = len(items)

        for i in range(1, count_visible + 1):
            code_text = ""
            code_xpath = RESULT_CARD_CODE_XPATH_TEMPLATE.format(i=i)
            link_xpath = RESULT_CARD_LINK_XPATH_TEMPLATE.format(i=i)

            try:
                code_elem = driver.find_element(By.XPATH, code_xpath)
                code_text = code_elem.text.strip()
                if code_text:
                    codes.append(code_text)
            except NoSuchElementException:
                pass

            print(f"  🔗 Producto {i}/{count_visible}: {code_text or 'SIN CÓDIGO'}")

            before_url = driver.current_url
            handles_before = set(driver.window_handles)

            try:
                link_elem = WebDriverWait(driver, 8).until(
                    EC.element_to_be_clickable((By.XPATH, link_xpath))
                )
                click_element(driver, link_elem)

                WebDriverWait(driver, 10).until(
                    lambda d: len(set(d.window_handles) - handles_before) > 0
                    or d.current_url != before_url
                    or len(d.find_elements(By.XPATH, DETAIL_IMAGES_CONTAINER_XPATH)) > 0
                )

                new_handles = list(set(driver.window_handles) - handles_before)
                if new_handles:
                    driver.switch_to.window(new_handles[0])

                detail_texts = extract_product_image_texts(driver, timeout=10)
                print(f"    📄 Textos OCR detectados: {len(detail_texts)}")

            except Exception as e:
                print(f"    ⚠️ Error procesando detalle del producto {i}: {e}")
            finally:
                try:
                    if len(driver.window_handles) > len(handles_before):
                        driver.close()
                        remaining = list(handles_before)
                        if remaining:
                            driver.switch_to.window(remaining[0])
                    else:
                        if driver.current_url != before_url:
                            driver.back()

                    WebDriverWait(driver, 10).until(
                        EC.presence_of_element_located((By.XPATH, RESULTS_CONTAINER_XPATH))
                    )
                except Exception as e:
                    print(f"    ⚠️ Error regresando al listado: {e}")
                    return codes

    except Exception as e:
        print(f"⚠️ Error extrayendo productos visibles: {e}")

    return codes


def extract_results_with_pagination(driver: webdriver.Chrome) -> list[str]:
    # Esperar al conteo de resultados
    total_items = 0
    try:
        count_element = WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.XPATH, "/html/body/section/div/div/div[2]/div/div/div/div[2]/div[1]/div[2]/div/div[2]/div[1]/h5"))
        )
        text = count_element.text
        match = re.search(r"de\s+(\d+)\s+resultados", text)
        if match:
            total_items = int(match.group(1))
            print(f"  Encontrados {total_items} items.")
        else:
            print(f"⚠️ No se pudo extraer el total de resultados del texto: '{text}'")
            return []
    except TimeoutException:
        print("⚠️ No se encontraron resultados (timeout esperando título de conteo).")
        return []

    # Extraer página 1
    all_codes = extract_visible_products(driver)
    
    # Calcular si hay más páginas
    # Cada página muestra hasta 24 resultados
    items_per_page = 24
    if total_items > items_per_page:
        total_pages = math.ceil(total_items / items_per_page)
        print(f"  Paginando {total_pages} páginas...")
        
        for page in range(2, total_pages + 1):
            try:
                # XPath de paginación: .../nav/ul/li[{i}]/a
                # Nota: Asumimos que el índice del <li> coincide con el número de página según instrucción del usuario
                pagination_xpath = f"/html/body/section/div/div/div[2]/div/div/div/div[2]/div[3]/div/div/div[1]/nav/ul/li[{page}]/a"
                
                next_page_btn = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable((By.XPATH, pagination_xpath))
                )
                click_element(driver, next_page_btn)
                time.sleep(3) # Esperar carga de nueva página
                
                # Extraer productos de la nueva página
                page_codes = extract_visible_products(driver)
                all_codes.extend(page_codes)
                
            except Exception as e:
                print(f"⚠️ Error en paginación página {page}: {e}")
                break
                
    return all_codes


def save_to_csv(data: dict, filename: str = "resultados.csv", include_codes: bool = True):
    file_exists = False
    try:
        with open(filename, "r", encoding="utf-8") as f:
            file_exists = True
    except FileNotFoundError:
        pass

    fieldnames = ["Segmento", "Marca", "Modelo", "Versión", "Año"]
    if include_codes:
        fieldnames.append("Códigos")
    fieldnames.append("processing_seconds")
    
    with open(filename, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        
        # Mapear los índices a nombres de columnas
        try:
            row = {
                "Segmento": data["filters"][0] if len(data["filters"]) > 0 else "",
                "Marca": data["filters"][1] if len(data["filters"]) > 1 else "",
                "Modelo": data["filters"][2] if len(data["filters"]) > 2 else "",
                "Versión": data["filters"][3] if len(data["filters"]) > 3 else "",
                "Año": data["filters"][4] if len(data["filters"]) > 4 else ""
            }
            if include_codes:
                row["Códigos"] = ", ".join(data.get("codes", []))
            processing_seconds = data.get("processing_seconds")
            row["processing_seconds"] = "" if processing_seconds is None else f"{processing_seconds:.4f}"
            writer.writerow(row)
            print(f"  💾 Guardado en {filename}")
        except Exception as e:
            print(f"⚠️ Error escribiendo fila en CSV: {e}")


def save_options_batch(results: list, current_indices: list[int], current_values: list[str], options_batch: list[tuple[int, str]], current_url: str, filename: str = "resultados.csv") -> None:
    file_exists = False
    try:
        with open(filename, "r", encoding="utf-8"):
            file_exists = True
    except FileNotFoundError:
        pass

    fieldnames = ["Segmento", "Marca", "Modelo", "Versión", "Año", "processing_seconds"]

    with open(filename, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()

        for option_index, option_text in options_batch:
            option_start = time.perf_counter()
            filters = current_values + [option_text]
            indices = current_indices + [option_index]
            result_data = {
                "indices": indices,
                "filters": filters,
                "codes": [],
                "url": current_url
            }
            processing_seconds = time.perf_counter() - option_start
            result_data["processing_seconds"] = processing_seconds
            results.append(result_data)

            row = {
                "Segmento": filters[0] if len(filters) > 0 else "",
                "Marca": filters[1] if len(filters) > 1 else "",
                "Modelo": filters[2] if len(filters) > 2 else "",
                "Versión": filters[3] if len(filters) > 3 else "",
                "Año": filters[4] if len(filters) > 4 else "",
                "processing_seconds": f"{processing_seconds:.4f}"
            }
            writer.writerow(row)

    print(f"  💾 Guardado en lote en {filename} ({len(options_batch)} combinaciones)")

def search_specific(driver: webdriver.Chrome, specific_indices: list[int], results: list, timeout: int, only_options: bool = False, ocr_max_images: int = 30, registered_codes: set[str] | None = None, application_codes: dict[str, list[str]] | None = None, csv_filename: str = RESULTADOS_FILENAME):
    print(f"🔍 Ejecutando búsqueda específica con índices: {specific_indices}")
    operation_start = time.perf_counter()
    
    current_values = []
    
    for i, index in enumerate(specific_indices):
        if i >= len(SELECT_XPATHS):
            print(f"⚠️ Se proporcionaron más índices que selects disponibles ({len(SELECT_XPATHS)}). Ignorando los extra.")
            break
            
        xpath = SELECT_XPATHS[i]
        
        try:
             # Esperar a que el select esté habilitado y tenga opciones
             # Usamos una espera explícita simple en el elemento
            WebDriverWait(driver, timeout).until(
                EC.element_to_be_clickable((By.XPATH, xpath))
            )
            
            # Esperar a que tenga opciones cargadas (más allá del placeholder)
            # Esto es crítico para los selects dependientes
            def select_has_options(d):
                sel = d.find_element(By.XPATH, xpath)
                return len(Select(sel).options) > 1
                
            WebDriverWait(driver, timeout).until(select_has_options)

            select_elem = driver.find_element(By.XPATH, xpath)
            select_obj = Select(select_elem)
            
            # Verificar índice válido
            if index < len(select_obj.options):
                option_text = select_obj.options[index].text
                print(f"  ➡️ Nivel {i+1}: Seleccionando '{option_text}' (índice {index})")
                select_obj.select_by_index(index)
                current_values.append(option_text)
                time.sleep(1.5) # Espera para que el siguiente select cargue sus opciones
            else:
                print(f"❌ Error: El índice {index} está fuera de rango para el select de nivel {i+1}. (Máximo: {len(select_obj.options)-1})")
                return
                
        except TimeoutException:
            print(f"❌ Timeout esperando el select de nivel {i+1}.")
            return
        except Exception as e:
            print(f"❌ Error inesperado seleccionando nivel {i+1}: {e}")
            return
            
    # Click en Buscar solo si hemos recorrido todos los índices solicitados
    if only_options:
        result_data = {
            "indices": specific_indices,
            "filters": current_values,
            "codes": [],
            "url": driver.current_url,
            "processing_seconds": time.perf_counter() - operation_start
        }
        results.append(result_data)
        print("🧾 Modo only-options: no se escribe en resultados.csv.")
        print("✅ Registro específico de opciones completado.")
        return

    try:
        print("  🌐 Extrayendo resultados vía HTTP...")
        codes, products, products_url, extraction_error = extract_results_via_http(driver, current_values, timeout)
        if extraction_error:
            raise RuntimeError(f"Extracción HTTP fallida para {current_values}: {extraction_error}")
        run_ocr_for_products(
            driver,
            timeout,
            products,
            ocr_max_images=ocr_max_images,
            registered_codes=registered_codes if registered_codes is not None else set(),
            application_codes=application_codes if application_codes is not None else {},
            csv_filename=csv_filename,
        )
        
        print(f"✅ Búsqueda específica completada. {len(codes)} códigos encontrados.")
        print(f"  Códigos: {codes}")
        
        result_data = {
            "indices": specific_indices,
            "filters": current_values,
            "codes": codes,
            "url": products_url or driver.current_url,
            "processing_seconds": time.perf_counter() - operation_start
        }
        results.append(result_data)
        if not codes:
            print("⚠️ No se encontraron códigos para guardar.")
            
    except Exception as e:
         print(f"❌ Error al ejecutar la búsqueda final: {e}")


def explore_combinations(driver: webdriver.Chrome, timeout: int, depth: int, current_indices: list[int], current_values: list[str], results: list, dev_mode: bool = False, resume_state: dict = None, only_options: bool = False, ocr_max_images: int = 30, registered_codes: set[str] | None = None, application_codes: dict[str, list[str]] | None = None, csv_filename: str = RESULTADOS_FILENAME) -> bool:
    # resume_state = {"skipping": bool, "target": list[str]}
    should_search = False
    
    # Verificar si el select actual existe y tiene opciones válidas
    if depth < len(SELECT_XPATHS):
        xpath = SELECT_XPATHS[depth]
        try:
            # Esperar brevemente a que el select esté presente
            # WebDriverWait(driver, 2).until(
            EC.presence_of_element_located((By.XPATH, xpath))
            # )
            select_elem = driver.find_element(By.XPATH, xpath)
            select_obj = Select(select_elem)
            
            if not select_elem.is_enabled() or len(select_obj.options) <= 1:
                should_search = True
            else:
                num_options = len(select_obj.options)
                state_dirtied = False
                start_index = 1

                if resume_state and resume_state.get("skipping", False) and depth < len(resume_state["target"]):
                    target_val = resume_state["target"][depth]
                    target_index = None
                    for idx in range(1, num_options):
                        if select_obj.options[idx].text.strip() == target_val:
                            target_index = idx
                            break

                    if target_index is not None:
                        print(f"  [RESUME] Saltando al hito '{target_val}' en nivel {depth+1}...")
                        select_obj.select_by_index(target_index)
                        time.sleep(1)

                        explore_combinations(driver, timeout, depth + 1,
                            current_indices + [target_index], current_values + [target_val],
                            results, dev_mode=dev_mode, resume_state=resume_state, only_options=only_options, ocr_max_images=ocr_max_images, registered_codes=registered_codes, application_codes=application_codes, csv_filename=csv_filename)

                        if resume_state.get("skipping", False):
                            resume_state["skipping"] = False
                            print("  [RESUME] Objetivo alcanzado y superado. Reanudando scraping normal.")

                        state_dirtied = True
                        start_index = target_index + 1
                    else:
                        print(f"  [RESUME] No se encontró '{target_val}' en nivel {depth+1}. Iniciando desde cero.")
                        resume_state["skipping"] = False

                if dev_mode:
                    limit = min(num_options, 3)
                    iterator_range = range(max(start_index, 1), limit)
                    if len(iterator_range) > 0:
                        print(f"  [DEV] Limitando nivel {depth+1} a {limit-1} opciones.")
                else:
                    iterator_range = range(start_index, num_options)

                if only_options and depth == len(SELECT_XPATHS) - 1:
                    print(f"{'  ' * depth}Nivel {depth + 1}: último select. Modo only-options sin escritura en resultados.csv.")
                    return state_dirtied

                for i in iterator_range:
                    if state_dirtied:
                        if not restore_select_path(driver, timeout, current_indices[:depth]):
                            navigate_back_to_form(driver, timeout)
                            if not restore_select_path(driver, timeout, current_indices[:depth]):
                                return True
                        state_dirtied = False

                    WebDriverWait(driver, timeout).until(
                        lambda d, xp=xpath: len(Select(d.find_element(By.XPATH, xp)).options) > 1
                    )
                    select_obj = Select(driver.find_element(By.XPATH, xpath))
                    option_text = select_obj.options[i].text.strip()

                    print(f"{'  ' * depth}Nivel {depth + 1}: Seleccionando '{option_text}' ({i}/{num_options-1})")
                    select_obj.select_by_index(i)
                    time.sleep(1)

                    if only_options and depth + 1 < len(SELECT_XPATHS):
                        has_deeper = False
                        for nd in range(depth + 1, len(SELECT_XPATHS)):
                            try:
                                ne = driver.find_element(By.XPATH, SELECT_XPATHS[nd])
                                ns = Select(ne)
                                if ne.is_enabled() and len(ns.options) > 1:
                                    has_deeper = True
                                    break
                            except Exception:
                                continue

                        if not has_deeper:
                            print(f"{'  ' * (depth+1)}Sin selects posteriores activos. Registrando combinación directa.")
                            result_data = {
                                "indices": current_indices + [i],
                                "filters": current_values + [option_text],
                                "codes": [],
                                "url": driver.current_url
                            }
                            results.append(result_data)
                            state_dirtied = True
                            continue

                    dirtied = explore_combinations(driver, timeout, depth + 1, current_indices + [i], current_values + [option_text], results, dev_mode=dev_mode, resume_state=resume_state, only_options=only_options, ocr_max_images=ocr_max_images, registered_codes=registered_codes, application_codes=application_codes, csv_filename=csv_filename)

                    if dirtied:
                        state_dirtied = True
                
                return state_dirtied

        except TimeoutException:
            # Si no aparece el select (raro, pero posible en DOM dinámico), asumimos fin de camino
            should_search = True
    else:
        # Se alcanzó la profundidad máxima (todos los selects seleccionados)
        should_search = True

    if should_search:
        # Lógica RESUME: Si estamos en modo skipping y llegamos aquí, significa que 
        # hemos recorrido el camino hasta el último elemento guardado.
        # No debemos volver a scrapear, solo marcar como visitado.
        if resume_state and resume_state.get("skipping", False):
            print(f"  [RESUME] Saltando scraping ya realizado para: {current_values}")
            return False

        if only_options:
            print(f"  Registrando combinación: {current_values}")
            operation_start = time.perf_counter()
            result_data = {
                "indices": current_indices.copy(),
                "filters": current_values.copy(),
                "codes": [],
                "url": driver.current_url,
                "processing_seconds": time.perf_counter() - operation_start
            }
            results.append(result_data)
            print("🧾 Modo only-options: no se escribe en resultados.csv.")
            return False

        print(f"  Ejecutando búsqueda para combinación: {current_values}")
        operation_start = time.perf_counter()
        try:
            codes, products, products_url, extraction_error = extract_results_via_http(driver, current_values, timeout)
            if extraction_error:
                raise RuntimeError(f"Extracción HTTP fallida para {current_values}: {extraction_error}")
            run_ocr_for_products(
                driver,
                timeout,
                products,
                ocr_max_images=ocr_max_images,
                registered_codes=registered_codes if registered_codes is not None else set(),
                application_codes=application_codes if application_codes is not None else {},
                csv_filename=csv_filename,
            )
            
            result_data = {
                "indices": current_indices.copy(),
                "filters": current_values.copy(),
                "codes": codes,
                "url": products_url or driver.current_url,
                "processing_seconds": time.perf_counter() - operation_start
            }
            results.append(result_data)
            return True
            
        except Exception as e:
            print(f"❌ Error al ejecutar búsqueda: {e}")
            return False

    return False


def scrape(
    url: str,
    selector: str,
    timeout: int,
    headless: bool,
    by_xpath: bool = False,
    keep_open: bool = True,
    dev_mode: bool = False,
    specific_indices: list[int] = None,
    resume: bool = False,
    only_options: bool = False,
    silent: bool = False,
    http_retries: int = 1,
    http_retry_delay: float = 1.5,
    ocr_max_images: int = 30
) -> dict:
    global HTTP_MAX_RETRIES
    global HTTP_RETRY_DELAY_SECONDS
    HTTP_MAX_RETRIES = max(0, http_retries)
    HTTP_RETRY_DELAY_SECONDS = max(0.0, http_retry_delay)

    csv_filename = RESULTADOS_FILENAME
    registered_codes = set()
    application_codes: dict[str, list[str]] = {}
    if not only_options:
        registered_codes = load_registered_codes(csv_filename)
        ensure_resultados_csv_header(csv_filename)
        application_codes = load_application_codes_map(csv_filename)
        print(f"📚 Códigos previamente registrados: {len(registered_codes)}")

    setup_ocr()
    driver = create_driver(headless=headless)
    target_url = LOGIN_URL
    result = {
        "url": target_url,
        "title": None,
        "success": False,
        "error": None,
        "data": []
    }
    
    resume_state = None
    if resume:
        last_filters = get_last_processed_filters()
        if last_filters:
            print(f"🔄 Modo RESUME activado. Último filtro detectado: {last_filters}")
            print("   Se saltarán todas las combinaciones anteriores a esta.")
            resume_state = {"skipping": True, "target": last_filters}
        else:
            print("⚠️ No se encontró historial válido en resultados.csv para reanudar. Se iniciará desde cero.")

    try:
        # Primero iniciamos sesión
        if not login(driver, timeout):
            result["error"] = "No se pudo iniciar sesión"
            return result

        print(f"Navegando a la URL objetivo: {target_url}")
        if driver.current_url.rstrip("/") != target_url.rstrip("/"):
            driver.get(target_url)

        try:
            vehicle_tab_timeout = min(timeout, 6)
            vehicle_tab = WebDriverWait(driver, vehicle_tab_timeout).until(
                EC.element_to_be_clickable((By.XPATH, VEHICULO_TAB_XPATH))
            )
            click_element(driver, vehicle_tab)
        except Exception as e:
            print(f"⚠️ No se pudo clickear la pestaña VEHÍCULO: {e}")

        combinations_results = []
        
        if specific_indices:
            search_specific(
                driver,
                specific_indices,
                combinations_results,
                timeout,
                only_options=only_options,
                ocr_max_images=ocr_max_images,
                registered_codes=registered_codes,
                application_codes=application_codes,
                csv_filename=csv_filename,
            )
            if only_options:
                result["title"] = f"Registro Específico de Opciones: {specific_indices}"
            else:
                result["title"] = f"Búsqueda Específica: {specific_indices}"
        else:
            explore_combinations(
                driver,
                timeout,
                0,
                [],
                [],
                combinations_results,
                dev_mode=dev_mode,
                resume_state=resume_state,
                only_options=only_options,
                ocr_max_images=ocr_max_images,
                registered_codes=registered_codes,
                application_codes=application_codes,
                csv_filename=csv_filename,
            )
            if only_options:
                result["title"] = "Registro Recursivo de Opciones Completado"
            else:
                result["title"] = "Exploración Recursiva Completada"
        
        result["data"] = combinations_results
        result["success"] = True
        
    except TimeoutException:
        result["error"] = f"Tiempo de espera agotado ({timeout}s) buscando el selector: {selector}"
    except WebDriverException as e:
        result["error"] = f"Error de red o del navegador: {e.msg}"
    except Exception as e:
        result["error"] = f"Error inesperado: {str(e)}"
    finally:
        if keep_open and not headless and not silent:
            print("Operación finalizada. Presiona Enter para cerrar el navegador.")
            input()
        driver.quit()
        
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scraping robusto con Selenium")
    parser.add_argument("url", help="Argumento mantenido por compatibilidad")
    parser.add_argument(
        "--selector",
        default="h1",
        help="Selector a extraer (por defecto: h1)",
    )
    parser.add_argument(
        "--xpath",
        action="store_true",
        help="Indica si el selector proporcionado es un XPath",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=10,
        help="Tiempo máximo de espera en segundos (por defecto: 10)",
    )
    parser.add_argument(
        "--visible",
        action="store_true",
        help="Abre el navegador en modo visible",
    )
    parser.add_argument(
        "--output",
        help="Ruta del archivo JSON para guardar los resultados",
    )
    parser.add_argument(
        "--auto-close",
        action="store_true",
        help="Cierra el navegador automáticamente al finalizar",
    )
    parser.add_argument(
        "--dev",
        action="store_true",
        help="Modo desarrollador: limita la exploración a las primeras opciones para pruebas rápidas"
    )
    parser.add_argument(
        "--specific",
        help="Lista de índices separados por coma para búsqueda específica (ej: '1,3,2')",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Reanuda la búsqueda desde el último resultado registrado en resultados.csv",
    )
    parser.add_argument(
        "--only-options",
        action="store_true",
        help="Registra solo combinaciones de opciones en CSV, sin buscar ni extraer códigos",
    )
    parser.add_argument(
        "--silent",
        action="store_true",
        help="No imprime nada en la terminal (silencio absoluto)",
    )
    parser.add_argument(
        "--http-retries",
        type=int,
        default=1,
        help="Cantidad de reintentos HTTP para extracción de listados (por defecto: 1)",
    )
    parser.add_argument(
        "--http-retry-delay",
        type=float,
        default=1.5,
        help="Segundos de espera entre reintentos HTTP (por defecto: 1.5)",
    )
    parser.add_argument(
        "--ocr-max-images",
        type=int,
        default=30,
        help="Tamaño de lote de imágenes OCR por producto; procesa todas en lotes de N (por defecto: 30)",
    )
    return parser.parse_args()


def main() -> None:
    pre_silent = "--silent" in sys.argv
    if pre_silent:
        enable_silent_mode()

    args = parse_args()

    if args.silent and not pre_silent:
        enable_silent_mode()
    
    specific_indices = None
    if args.specific:
        try:
            specific_indices = [int(x.strip()) for x in args.specific.split(",")]
        except ValueError:
            print("Error: El argumento --specific debe ser una lista de números separados por coma.")
            sys.exit(1)
    
    print(f"Iniciando scraping de: {args.url}")
    if args.dev: 
        print("🛠️ MODO DESARROLLADOR ACTIVADO: Exploración limitada.")
    if specific_indices:
        print(f"🎯 MODO BÚSQUEDA ESPECÍFICA: Índices {specific_indices}")
    if args.resume:
        print("🔄 MODO RESUME ACTIVADO: Se buscará retomar desde la última combinación guardada.")
    if args.only_options:
        print("🧾 MODO ONLY OPTIONS ACTIVADO: Se registrarán solo combinaciones, sin códigos.")

    result = scrape(
        url=args.url,
        selector=args.selector,
        timeout=args.timeout,
        headless=not args.visible,
        by_xpath=args.xpath,
        keep_open=not args.auto_close,
        dev_mode=args.dev,
        specific_indices=specific_indices,
        resume=args.resume,
        only_options=args.only_options,
        silent=args.silent,
        http_retries=args.http_retries,
        http_retry_delay=args.http_retry_delay,
        ocr_max_images=args.ocr_max_images
    )
    
    if result["success"]:
        print(f"✅ Éxito! Título: {result['title']}")
        print(f"Encontrados {len(result['data'])} elementos.")
        
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(result["data"], f, ensure_ascii=False, indent=2) # Guardamos solo data por compatibilidad con otros json
            print(f"Resultados guardados en: {args.output}")
        else:
            pass # Ya se imprimen en consola durante la ejecución
    else:
        print(f"❌ Error: {result['error']}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()