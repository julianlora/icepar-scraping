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
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

try:
    import pytesseract
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps
except Exception:
    pytesseract = None
    Image = None
    ImageEnhance = None
    ImageFilter = None
    ImageOps = None

LOGIN_URL = "https://icepar.specparts.shop/"
PRODUCTS_URL = "https://icepar.specparts.shop/products"
EMAIL = "ecolarusso@icepar-sa.com.ar"
PASSWORD = "piero25*"

EMAIL_XPATH = "/html/body/section/div/div/div[1]/div/form/div[1]/input"
PASSWORD_XPATH = "/html/body/section/div/div/div[1]/div/form/div[2]/div/input"
SUBMIT_XPATH = "/html/body/section/div/div/div[1]/div/form/div[3]/div[2]/button"
DETAIL_IMAGES_CONTAINER_XPATH = "/html/body/section[2]/div/div/div[1]/div/div[1]/div/div"
DETAIL_SECTION_LABELS = {
    "aplicaciones": "APLICACIONES",
    "atributos": "ATRIBUTOS",
    "referencias": "REFERENCIAS",
    # "oem": "OEM",
}
DETAIL_SECTION_EMPTY_MESSAGES = {
    "aplicaciones": "No hay aplicaciones para mostrar",
}

_XPATH_TRANSLATE_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZÁÉÍÓÚÜÑ"
_XPATH_TRANSLATE_LOWER = "abcdefghijklmnopqrstuvwxyzáéíóúüñ"

ITEMS_PER_PAGE = 24
OUTPUT_FILENAME = "resultados_productos_ocr.csv"
FAILED_FILENAME = "resultados_productos_ocr_failed.csv"
OUTPUT_FILENAME_EXCLUDE_ARTS = "resultados_productos_ocr_no_arts.csv"
FAILED_FILENAME_EXCLUDE_ARTS = "resultados_productos_ocr_no_arts_failed.csv"
ARTS_FILENAME = "ARTS_ICEPAR.csv"
HTTP_CONNECT_TIMEOUT_SECONDS = 10

_DEVNULL_STREAM = None
_OCR_READY = False


def build_ranged_output_filename(base_filename: str, start_page: int, end_page: int | None) -> str:
    root, ext = os.path.splitext(base_filename)
    page_suffix = f"_pages_{start_page}_{end_page}" if end_page is not None else f"_pages_{start_page}_end"
    return f"{root}{page_suffix}{ext}"


def resolve_default_output_filenames(exclude_arts: bool) -> tuple[str, str]:
    if exclude_arts:
        return OUTPUT_FILENAME_EXCLUDE_ARTS, FAILED_FILENAME_EXCLUDE_ARTS
    return OUTPUT_FILENAME, FAILED_FILENAME


def _normalize_code(value: str) -> str:
    return " ".join((value or "").strip().split())


def _normalize_text(value: str) -> str:
    return " ".join((value or "").split()).strip()


def _normalize_ocr_text(value: str) -> str:
    normalized = (value or "").replace("|", "I").replace("[", "(").replace("]", ")")
    normalized = re.sub(r"(?<=[A-Za-zÁÉÍÓÚÜÑáéíóúüñ])5(?=[A-Za-zÁÉÍÓÚÜÑáéíóúüñ])", "S", normalized)
    normalized = re.sub(r"(?<=[A-Za-zÁÉÍÓÚÜÑáéíóúüñ])0(?=[A-Za-zÁÉÍÓÚÜÑáéíóúüñ])", "O", normalized)
    normalized = re.sub(r"(?<=[A-Za-zÁÉÍÓÚÜÑáéíóúüñ])1(?=[A-Za-zÁÉÍÓÚÜÑáéíóúüñ])", "I", normalized)
    normalized = re.sub(r"(?<=[A-Za-zÁÉÍÓÚÜÑáéíóúüñ])8(?=[A-Za-zÁÉÍÓÚÜÑáéíóúüñ])", "B", normalized)
    return _normalize_text(normalized)


def _dedupe_keep_order(values: list[str]) -> list[str]:
    seen = set()
    output = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        output.append(value)
    return output


def enable_silent_mode() -> None:
    global _DEVNULL_STREAM
    if _DEVNULL_STREAM is None:
        _DEVNULL_STREAM = open(os.devnull, "w", encoding="utf-8")
    sys.stdout = _DEVNULL_STREAM
    sys.stderr = _DEVNULL_STREAM


def setup_ocr() -> bool:
    global _OCR_READY
    if pytesseract is None or Image is None:
        print("OCR no disponible: faltan dependencias de Python (pytesseract/Pillow).")
        _OCR_READY = False
        return False

    if shutil.which("tesseract") is None:
        print("OCR no disponible: no se encontro Tesseract en PATH.")
        _OCR_READY = False
        return False

    _OCR_READY = True
    print("OCR inicializado con Tesseract.")
    return True


def ocr_text_from_image_bytes(image_bytes: bytes) -> str:
    if not _OCR_READY:
        return ""

    try:
        image = Image.open(io.BytesIO(image_bytes))
        image.load()

        base_image = image.convert("RGB")
        grayscale = ImageOps.grayscale(base_image)
        border_x = max(20, grayscale.width // 8)
        border_y = max(12, grayscale.height // 3)
        padded = ImageOps.expand(grayscale, border=(border_x, border_y), fill=255)
        enlarged = padded.resize(
            (max(1, padded.width * 3), max(1, padded.height * 3)),
            Image.Resampling.LANCZOS,
        )
        contrasted = ImageEnhance.Contrast(enlarged).enhance(2.5)
        
        # sharpened = contrasted.filter(ImageFilter.SHARPEN)
        # thresholded = sharpened.point(lambda pixel: 255 if pixel > 180 else 0, mode="1")
        # inverted = ImageOps.invert(sharpened)
        # inverted_thresholded = inverted.point(lambda pixel: 255 if pixel > 180 else 0, mode="1")

        # variants = [
        #     base_image,
        #     grayscale,
        #     enlarged,
        #     sharpened,
        #     thresholded,
        #     inverted,
        #     inverted_thresholded,
        # ]
        # configs = [
        #     "--psm 6",
        #     "--psm 11",
        #     "--psm 12",
        # ]

        # best_text = ""
        # for variant in variants:
        #     for config in configs:
        #         text = _normalize_ocr_text(pytesseract.image_to_string(variant, config=config))
        #         if len(text) > len(best_text):
        #             best_text = text
        #         if text:
        #             return text

        best_text = _normalize_ocr_text(pytesseract.image_to_string(contrasted, config="--psm 6"))
        return best_text
    except Exception as e:
        print(f"    Error OCR en imagen: {e}")
        return ""


def _download_image_bytes_for_ocr(
    image_url: str,
    http_session: requests.Session | None,
    referer_url: str,
    timeout: int,
) -> bytes:
    normalized_url = urljoin(referer_url or PRODUCTS_URL, image_url or "")
    if not normalized_url.startswith("http"):
        return b""

    sessions_to_try = []
    if http_session is not None:
        sessions_to_try.append(http_session)
    sessions_to_try.append(requests.Session())

    headers = {
        "Referer": referer_url or LOGIN_URL,
        "User-Agent": "Mozilla/5.0",
    }

    for session in sessions_to_try:
        try:
            response = session.get(normalized_url, timeout=timeout, headers=headers)
            if response.status_code == 200 and response.content:
                return response.content
        except Exception as e:
            print(f"    Error descargando imagen para OCR: {e}")

    return b""


def ocr_text_from_image_element(image_element, http_session: requests.Session | None = None, timeout: int = 10) -> str:
    if not _OCR_READY:
        return ""

    image_url = ""
    referer_url = ""
    try:
        image_url = (
            image_element.get_attribute("currentSrc")
            or image_element.get_attribute("src")
            or image_element.get_attribute("data-src")
            or ""
        ).strip()
    except Exception:
        image_url = ""

    try:
        referer_url = image_element.parent.execute_script("return window.location.href;")
    except Exception:
        referer_url = ""

    if image_url:
        image_bytes = _download_image_bytes_for_ocr(
            image_url=image_url,
            http_session=http_session,
            referer_url=referer_url,
            timeout=timeout,
        )
        if image_bytes:
            text = ocr_text_from_image_bytes(image_bytes)
            if text:
                return text

    try:
        width = float(image_element.rect.get("width") or 0)
        height = float(image_element.rect.get("height") or 0)
        if width <= 0 or height <= 0:
            return ""
        png_bytes = image_element.screenshot_as_png
        return ocr_text_from_image_bytes(png_bytes)
    except Exception as e:
        print(f"    Error OCR en imagen: {e}")
        return ""


def _get_detail_container(driver: webdriver.Chrome, timeout: int):
    return WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((By.XPATH, DETAIL_IMAGES_CONTAINER_XPATH))
    )


def _collect_detail_images(driver: webdriver.Chrome, timeout: int) -> list:
    try:
        container = _get_detail_container(driver, timeout)
    except TimeoutException:
        return []

    deadline = time.perf_counter() + max(2, timeout)
    stable_rounds = 0
    last_sources: list[str] = []
    best_images = []

    while time.perf_counter() < deadline:
        raw_images = container.find_elements(By.XPATH, ".//img")
        current_images = []
        current_sources = []
        seen_sources = set()

        for image in raw_images:
            try:
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", image)
            except Exception:
                pass

            try:
                src = (image.get_attribute("src") or image.get_attribute("data-src") or "").strip()
                if src:
                    if src in seen_sources:
                        continue
                    seen_sources.add(src)
                    current_sources.append(src)
                current_images.append(image)
            except Exception:
                continue

        if len(current_images) > len(best_images):
            best_images = current_images

        if current_sources == last_sources and current_sources:
            stable_rounds += 1
        else:
            stable_rounds = 0
            last_sources = current_sources

        if stable_rounds >= 2:
            return current_images

        time.sleep(0.6)

    return best_images


def extract_product_image_texts(driver: webdriver.Chrome, timeout: int, http_session: requests.Session | None = None, ocr_max_images: int = 30) -> list[str]:
    images = _collect_detail_images(driver, timeout)
    if not images:
        try:
            _get_detail_container(driver, timeout)
        except TimeoutException:
            print("    No se encontro contenedor de imagenes del detalle.")
            return []

    if not images:
        print("    No hay imagenes en el detalle.")
        return []

    extracted_texts = []
    batch_size = max(1, ocr_max_images)
    total_images = len(images)

    for batch_start in range(0, total_images, batch_size):
        batch_end = min(batch_start + batch_size, total_images)
        print(f"    Procesando lote de imagenes {batch_start + 1}-{batch_end} de {total_images}...")
        batch_images = images[batch_start:batch_end]
        for offset, image in enumerate(batch_images, start=1):
            img_index = batch_start + offset
            text = ocr_text_from_image_element(image, http_session=http_session, timeout=timeout)
            extracted_texts.append(text)
            if text:
                print(f"    OCR imagen {img_index}/{total_images}: {text}")
            else:
                print(f"    OCR imagen {img_index}/{total_images}: (sin texto)")

    return extracted_texts


def click_element(driver: webdriver.Chrome, element) -> None:
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
    try:
        element.click()
    except Exception:
        driver.execute_script("arguments[0].click();", element)


def _locate_detail_section_tab(driver: webdriver.Chrome, label: str):
    normalized_label = _normalize_text(label).lower()
    xpaths = [
        (
            f"//*[@id='top-tab']//*[self::a or self::button][contains(@class, 'nav-link') and "
            f"translate(normalize-space(.), '{_XPATH_TRANSLATE_UPPER}', "
            f"'{_XPATH_TRANSLATE_LOWER}')='{normalized_label}']"
        ),
        (
            f"//*[@id='top-tab']//*[self::a or self::button][@role='tab' and "
            f"translate(normalize-space(.), '{_XPATH_TRANSLATE_UPPER}', "
            f"'{_XPATH_TRANSLATE_LOWER}')='{normalized_label}']"
        ),
        (
            f"//*[@id='top-tab']//li[contains(@class, 'nav-item')]//*[self::a or self::button]["
            f"translate(normalize-space(.), '{_XPATH_TRANSLATE_UPPER}', "
            f"'{_XPATH_TRANSLATE_LOWER}')='{normalized_label}']"
        ),
        (
            f"//*[@id='top-tab']//*[contains(@class, 'nav-link')]//*[self::span or self::i]/ancestor::*[self::a or self::button][1]["
            f"translate(normalize-space(.), '{_XPATH_TRANSLATE_UPPER}', "
            f"'{_XPATH_TRANSLATE_LOWER}')='{normalized_label}']"
        ),
    ]
    for xpath in xpaths:
        elements = driver.find_elements(By.XPATH, xpath)
        for element in elements:
            try:
                if element.is_displayed():
                    return element
            except Exception:
                continue
    return None


def _get_detail_section_pane(driver: webdriver.Chrome, tab, timeout: int):
    pane_selector = ""
    try:
        pane_selector = (tab.get_attribute("href") or tab.get_attribute("data-bs-target") or "").strip()
    except Exception:
        pane_selector = ""

    if pane_selector.startswith("#"):
        pane_id = pane_selector[1:]
        return WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.ID, pane_id))
        )

    return WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, ".tab-content .tab-pane.active, .tab-content .tab-pane.show.active"))
    )


def _pane_has_empty_message(pane, message: str) -> bool:
    normalized_message = _normalize_text(message)
    if not normalized_message:
        return False

    for paragraph in pane.find_elements(By.XPATH, ".//p"):
        try:
            if _normalize_text(paragraph.text) == normalized_message:
                return True
        except Exception:
            continue

    return False


def _collect_detail_images_from_pane(driver: webdriver.Chrome, pane, timeout: int, empty_message: str | None = None) -> list:
    if empty_message and _pane_has_empty_message(pane, empty_message):
        return []

    deadline = time.perf_counter() + max(2, timeout)
    stable_rounds = 0
    last_sources: list[str] = []
    best_images = []

    while time.perf_counter() < deadline:
        if empty_message and _pane_has_empty_message(pane, empty_message):
            return []

        raw_images = pane.find_elements(By.XPATH, ".//img")
        current_images = []
        current_sources = []
        seen_sources = set()

        for image in raw_images:
            try:
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", image)
            except Exception:
                pass

            try:
                src = (image.get_attribute("src") or image.get_attribute("data-src") or "").strip()
                if src:
                    if src in seen_sources:
                        continue
                    seen_sources.add(src)
                    current_sources.append(src)
                current_images.append(image)
            except Exception:
                continue

        if len(current_images) > len(best_images):
            best_images = current_images

        if current_sources == last_sources and current_sources:
            stable_rounds += 1
        else:
            stable_rounds = 0
            last_sources = current_sources

        if stable_rounds >= 2:
            return current_images

        time.sleep(0.6)

    return best_images


def open_detail_section(driver: webdriver.Chrome, label: str, timeout: int, empty_message: str | None = None):
    tab = _locate_detail_section_tab(driver, label)
    if tab is None:
        print(f"    No se encontro la pestana {label}.")
        return None

    try:
        click_element(driver, tab)
        time.sleep(0.8)
        pane = _get_detail_section_pane(driver, tab, timeout)
        _collect_detail_images_from_pane(driver, pane, timeout, empty_message=empty_message)
        return pane
    except Exception as e:
        print(f"    No se pudo abrir la pestana {label}: {e}")
        return None


def extract_product_detail_sections(
    driver: webdriver.Chrome,
    timeout: int,
    http_session: requests.Session | None = None,
    ocr_max_images: int = 30,
) -> dict[str, list[str]]:
    section_results: dict[str, list[str]] = {
        "aplicaciones": [],
        "atributos": [],
        "referencias": [],
        # "oem": [],
    }

    for key, label in DETAIL_SECTION_LABELS.items():
        print(f"    Extrayendo pestana {label}...")
        empty_message = DETAIL_SECTION_EMPTY_MESSAGES.get(key)
        pane = open_detail_section(driver, label, timeout, empty_message=empty_message)
        if pane is None:
            continue

        if empty_message and _pane_has_empty_message(pane, empty_message):
            print(f"    {empty_message}.")
            continue

        images = _collect_detail_images_from_pane(driver, pane, timeout, empty_message=empty_message)
        if not images:
            print("    No hay imagenes en el detalle.")
            continue

        detail_texts = []
        batch_size = max(1, ocr_max_images)
        total_images = len(images)

        for batch_start in range(0, total_images, batch_size):
            batch_end = min(batch_start + batch_size, total_images)
            print(f"    Procesando lote de imagenes {batch_start + 1}-{batch_end} de {total_images}...")
            batch_images = images[batch_start:batch_end]
            for offset, image in enumerate(batch_images, start=1):
                img_index = batch_start + offset
                text = ocr_text_from_image_element(image, http_session=http_session, timeout=timeout)
                detail_texts.append(text)
                if text:
                    print(f"    OCR imagen {img_index}/{total_images}: {text}")
                else:
                    print(f"    OCR imagen {img_index}/{total_images}: (sin texto)")

        normalized_texts = []
        seen_texts = set()
        for text in detail_texts:
            normalized = _normalize_text(text)
            if not normalized or normalized in seen_texts:
                continue
            seen_texts.add(normalized)
            normalized_texts.append(normalized)
        section_results[key] = normalized_texts

    return section_results


def create_driver(headless: bool) -> webdriver.Chrome:
    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-gpu")
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    prefs = {"profile.managed_default_content_settings.images": 2}
    options.add_experimental_option("prefs", prefs)
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
            print("Formulario de login no detectado. Continuando con sesion activa.")
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
        print("Sesion iniciada correctamente.")
        return True
    except TimeoutException:
        print("Formulario de login no detectado. Continuando con sesion activa.")
        return True
    except NoSuchElementException as e:
        print(f"Error al iniciar sesion: {e}")
        return False
    except Exception as e:
        print(f"Error al iniciar sesion: {e}")
        return False


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
        "Connection": "close",
    })
    return session


def build_products_page_url(page: int) -> str:
    return f"{PRODUCTS_URL}?page={page}"


def extract_total_items_from_products_html(html: str) -> int:
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    match = re.search(r"de\s+(\d+)\s+resultados", text, flags=re.IGNORECASE)
    if not match:
        return 0
    return int(match.group(1))


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


def load_arts_index(filename: str = ARTS_FILENAME) -> dict[str, dict[str, str]]:
    arts_index: dict[str, dict[str, str]] = {}
    with open(filename, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            code = _normalize_code(row.get("ARTS_ARTICULO_EMP", ""))
            if not code:
                continue
            arts_index[code] = {
                "ARTS_ARTICULO": row.get("ARTS_ARTICULO", ""),
                "ARTS_ARTICULO_EMP": row.get("ARTS_ARTICULO_EMP", ""),
                "ARTS_NOMBRE": row.get("ARTS_NOMBRE", ""),
                "ARTS_DESCRIPCION": row.get("ARTS_DESCRIPCION", ""),
            }
    return arts_index


def should_process_product(
    code: str,
    processed_codes: set[str],
    arts_index: dict[str, dict[str, str]],
    exclude_arts: bool,
) -> bool:
    if not code or code in processed_codes:
        return False
    code_exists_in_arts = code in arts_index
    if exclude_arts:
        return not code_exists_in_arts
    return code_exists_in_arts


def ensure_output_csv_header(filename: str = OUTPUT_FILENAME) -> None:
    fieldnames = output_fieldnames()
    if os.path.exists(filename):
        with open(filename, "r", newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            first_row = next(reader, None)
        if first_row == fieldnames:
            return
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        backup_name = f"{os.path.splitext(filename)[0]}_legacy_{timestamp}.csv"
        shutil.move(filename, backup_name)
        print(f"Formato CSV anterior detectado. Backup generado: {backup_name}")

    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()


def output_fieldnames() -> list[str]:
    return [
        "codigo",
        "detail_url",
        "page",
        "arts_articulo",
        "arts_articulo_emp",
        "arts_nombre",
        "arts_descripcion",
        "aplicaciones_ocr",
        "aplicaciones_ocr_count",
        "atributos_ocr",
        "atributos_ocr_count",
        "referencias_ocr",
        "referencias_ocr_count",
        "oem_ocr",
        "oem_ocr_count",
        "processing_seconds",
    ]


def ensure_failed_csv_header(filename: str = FAILED_FILENAME) -> None:
    fieldnames = ["codigo", "detail_url", "page", "error", "processing_seconds"]
    if os.path.exists(filename):
        return
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()


def load_processed_codes(filename: str = OUTPUT_FILENAME) -> set[str]:
    processed_codes = set()
    if not os.path.exists(filename):
        return processed_codes
    try:
        with open(filename, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                code = _normalize_code(row.get("codigo", ""))
                if code:
                    processed_codes.add(code)
    except Exception as e:
        print(f"Error leyendo codigos ya procesados: {e}")
    return processed_codes


def load_last_processed_page(filename: str = OUTPUT_FILENAME) -> int | None:
    if not os.path.exists(filename):
        return None

    last_page = None
    try:
        with open(filename, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                raw_page = (row.get("page") or "").strip()
                if not raw_page:
                    continue
                try:
                    page = int(raw_page)
                except ValueError:
                    continue
                if last_page is None or page > last_page:
                    last_page = page
    except Exception as e:
        print(f"Error leyendo ultima pagina procesada: {e}")

    return last_page


def append_output_row(row: dict[str, str], filename: str = OUTPUT_FILENAME) -> None:
    ensure_output_csv_header(filename)
    with open(filename, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=output_fieldnames())
        writer.writerow(row)


def append_failed_row(row: dict[str, str], filename: str = FAILED_FILENAME) -> None:
    ensure_failed_csv_header(filename)
    fieldnames = ["codigo", "detail_url", "page", "error", "processing_seconds"]
    with open(filename, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writerow(row)


def fetch_products_page(session: requests.Session, page: int, timeout: int) -> requests.Response:
    request_timeout = (min(HTTP_CONNECT_TIMEOUT_SECONDS, max(1, timeout)), max(1, timeout))
    url = build_products_page_url(page)
    response = session.get(url, timeout=request_timeout)
    response.raise_for_status()
    return response


def process_product_detail(
    driver: webdriver.Chrome,
    timeout: int,
    http_session: requests.Session,
    product: dict[str, str],
    arts_row: dict[str, str],
    page: int,
    ocr_max_images: int,
    output_filename: str,
    failed_filename: str,
) -> dict[str, str] | None:
    code = _normalize_code(product.get("code", ""))
    detail_url = (product.get("detail_url") or "").strip()
    started_at = time.perf_counter()

    if not code or not detail_url:
        append_failed_row({
            "codigo": code,
            "detail_url": detail_url,
            "page": str(page),
            "error": "Producto sin codigo o URL de detalle",
            "processing_seconds": f"{time.perf_counter() - started_at:.4f}",
        }, filename=failed_filename)
        return None

    try:
        print(f"  Detalle producto {code}: {detail_url}")
        driver.get(detail_url)
        section_texts = extract_product_detail_sections(
            driver,
            timeout=timeout,
            http_session=http_session,
            ocr_max_images=ocr_max_images,
        )

        processing_seconds = time.perf_counter() - started_at
        row = {
            "codigo": code,
            "detail_url": detail_url,
            "page": str(page),
            "arts_articulo": arts_row.get("ARTS_ARTICULO", ""),
            "arts_articulo_emp": arts_row.get("ARTS_ARTICULO_EMP", ""),
            "arts_nombre": arts_row.get("ARTS_NOMBRE", ""),
            "arts_descripcion": arts_row.get("ARTS_DESCRIPCION", ""),
            "aplicaciones_ocr": json.dumps(section_texts["aplicaciones"], ensure_ascii=False),
            "aplicaciones_ocr_count": str(len(section_texts["aplicaciones"])),
            "atributos_ocr": json.dumps(section_texts["atributos"], ensure_ascii=False),
            "atributos_ocr_count": str(len(section_texts["atributos"])),
            "referencias_ocr": json.dumps(section_texts["referencias"], ensure_ascii=False),
            "referencias_ocr_count": str(len(section_texts["referencias"])),
            # "oem_ocr": json.dumps(section_texts["oem"], ensure_ascii=False),
            # "oem_ocr_count": str(len(section_texts["oem"])),
            "processing_seconds": f"{processing_seconds:.4f}",
        }
        append_output_row(row, filename=output_filename)
        return row
    except Exception as e:
        append_failed_row({
            "codigo": code,
            "detail_url": detail_url,
            "page": str(page),
            "error": str(e),
            "processing_seconds": f"{time.perf_counter() - started_at:.4f}",
        }, filename=failed_filename)
        print(f"  Error procesando detalle {code}: {e}")
        return None


def scrape_products(
    timeout: int,
    headless: bool,
    keep_open: bool,
    silent: bool,
    ocr_max_images: int,
    output_filename: str,
    failed_filename: str,
    arts_filename: str,
    start_page: int,
    end_page: int | None,
    exclude_arts: bool,
) -> dict:
    if not setup_ocr():
        return {
            "url": PRODUCTS_URL,
            "title": "Scraping paginado de productos con OCR",
            "success": False,
            "error": "OCR no disponible en el entorno actual",
            "data": [],
        }
    ensure_output_csv_header(output_filename)
    ensure_failed_csv_header(failed_filename)
    processed_codes = load_processed_codes(output_filename)
    last_processed_page = load_last_processed_page(output_filename)
    arts_index = load_arts_index(arts_filename)
    filter_mode = "exclude_arts" if exclude_arts else "include_arts"
    filter_mode_description = "codigos que NO aparecen en ARTS" if exclude_arts else "codigos que SI aparecen en ARTS"
    print(f"Codigos en ARTS cargados: {len(arts_index)}")
    print(f"Codigos ya procesados: {len(processed_codes)}")
    print(f"Criterio de scraping: {filter_mode_description}")
    if last_processed_page is not None:
        print(f"Ultima pagina guardada en resultados: {last_processed_page}")
        print(f"Si necesitas retomar, usa --start-page {last_processed_page}")

    driver = create_driver(headless=headless)
    result = {
        "url": PRODUCTS_URL,
        "title": "Scraping paginado de productos con OCR",
        "success": False,
        "error": None,
        "filter_mode": filter_mode,
        "data": [],
    }

    try:
        if not login(driver, timeout):
            result["error"] = "No se pudo iniciar sesion"
            return result

        session = build_http_session_from_driver(driver)
        first_response = fetch_products_page(session, start_page, timeout)
        total_items = extract_total_items_from_products_html(first_response.text)
        if total_items:
            total_pages = math.ceil(total_items / ITEMS_PER_PAGE)
        else:
            total_pages = start_page
        target_end_page = min(end_page, total_pages) if end_page is not None else total_pages

        print(f"Total de productos reportados: {total_items}")
        print(f"Rango de paginas a procesar: {start_page} a {target_end_page}")

        page_response = first_response
        for page in range(start_page, target_end_page + 1):
            if page == start_page:
                current_response = page_response
            else:
                current_response = fetch_products_page(session, page, timeout)

            print(f"Procesando pagina {page}/{target_end_page}: {current_response.url}")
            page_products = extract_products_from_products_html(current_response.text, current_response.url)
            print(f"  Productos detectados en pagina: {len(page_products)}")

            valid_products = []
            for product in page_products:
                code = _normalize_code(product.get("code", ""))
                if not should_process_product(code, processed_codes, arts_index, exclude_arts):
                    continue
                valid_products.append(product)

            if exclude_arts:
                print(f"  Productos que NO existen en ARTS y faltan procesar: {len(valid_products)}")
            else:
                print(f"  Productos que existen en ARTS y faltan procesar: {len(valid_products)}")

            for product in valid_products:
                code = _normalize_code(product.get("code", ""))
                row = process_product_detail(
                    driver,
                    timeout,
                    session,
                    product,
                    arts_index.get(code, {}),
                    page,
                    ocr_max_images,
                    output_filename,
                    failed_filename,
                )
                if row is not None:
                    processed_codes.add(code)
                    result["data"].append(row)

        result["success"] = True
        return result
    except requests.HTTPError as e:
        result["error"] = f"Error HTTP: {e}"
        return result
    except WebDriverException as e:
        result["error"] = f"Error del navegador: {e.msg}"
        return result
    except Exception as e:
        result["error"] = f"Error inesperado: {e}"
        return result
    finally:
        if keep_open and not headless and not silent:
            print("Operacion finalizada. Presiona Enter para cerrar el navegador.")
            input()
        driver.quit()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scraping paginado de productos con OCR")
    parser.add_argument("url", help="Argumento mantenido por compatibilidad")
    parser.add_argument(
        "--timeout",
        type=int,
        default=15,
        help="Tiempo maximo de espera en segundos (por defecto: 15)",
    )
    parser.add_argument(
        "--visible",
        action="store_true",
        help="Abre el navegador en modo visible",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="CSV de salida detallado. Si se omite, usa un nombre distinto segun el modo seleccionado.",
    )
    parser.add_argument(
        "--failed-output",
        default=None,
        help="CSV de errores. Si se omite, usa un nombre distinto segun el modo seleccionado.",
    )
    parser.add_argument(
        "--arts-file",
        default=ARTS_FILENAME,
        help=f"CSV maestro de articulos (por defecto: {ARTS_FILENAME})",
    )
    parser.add_argument(
        "--auto-close",
        action="store_true",
        help="Cierra el navegador automaticamente al finalizar",
    )
    parser.add_argument(
        "--silent",
        action="store_true",
        help="No imprime nada en la terminal",
    )
    parser.add_argument(
        "--ocr-max-images",
        type=int,
        default=30,
        help="Tamano de lote de imagenes OCR por producto (por defecto: 30)",
    )
    parser.add_argument(
        "--start-page",
        type=int,
        default=1,
        help="Primera pagina a procesar (por defecto: 1)",
    )
    parser.add_argument(
        "--end-page",
        type=int,
        help="Ultima pagina a procesar. Si se omite, procesa hasta el final.",
    )
    parser.add_argument(
        "--json-output",
        help="Ruta opcional para guardar un resumen JSON de la corrida",
    )
    parser.add_argument(
        "--exclude-arts",
        action="store_true",
        help="Procesa solo productos cuyos codigos no existen en ARTS_ICEPAR.csv",
    )
    return parser.parse_args()


def main() -> None:
    pre_silent = "--silent" in sys.argv
    if pre_silent:
        enable_silent_mode()

    args = parse_args()

    if args.silent and not pre_silent:
        enable_silent_mode()

    if args.start_page < 1:
        print("Error: --start-page debe ser mayor o igual a 1.")
        sys.exit(1)
    if args.end_page is not None and args.end_page < args.start_page:
        print("Error: --end-page no puede ser menor que --start-page.")
        sys.exit(1)

    default_output_filename, default_failed_filename = resolve_default_output_filenames(args.exclude_arts)

    resolved_output = args.output or build_ranged_output_filename(
        default_output_filename,
        args.start_page,
        args.end_page,
    )
    resolved_failed_output = args.failed_output or build_ranged_output_filename(
        default_failed_filename,
        args.start_page,
        args.end_page,
    )

    print(f"Iniciando scraping de productos desde: {args.url}")
    if args.exclude_arts:
        print("Modo de scraping: productos fuera de ARTS")
    else:
        print("Modo de scraping: productos presentes en ARTS")
    print(f"Salida CSV: {resolved_output}")
    print(f"Salida errores: {resolved_failed_output}")

    result = scrape_products(
        timeout=args.timeout,
        headless=not args.visible,
        keep_open=not args.auto_close,
        silent=args.silent,
        ocr_max_images=args.ocr_max_images,
        output_filename=resolved_output,
        failed_filename=resolved_failed_output,
        arts_filename=args.arts_file,
        start_page=args.start_page,
        end_page=args.end_page,
        exclude_arts=args.exclude_arts,
    )

    if result["success"]:
        print(f"Exito. Productos guardados en esta corrida: {len(result['data'])}")
        if args.json_output:
            with open(args.json_output, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            print(f"Resumen JSON guardado en: {args.json_output}")
    else:
        print(f"Error: {result['error']}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()