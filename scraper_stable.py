import argparse
import csv
import json
import math
import os
import re
import sys
import time

from selenium import webdriver
from selenium.common.exceptions import ElementClickInterceptedException, NoSuchElementException, TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait

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

_DEVNULL_STREAM = None


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
                # Header only or empty
                return None
            last_row = lines[-1]
            # Strip whitespace from filter values
            return [val.strip() for val in last_row[:5] if val.strip()]
    except FileNotFoundError:
        return None
    except Exception as e:
        print(f"⚠️ Error leyendo archivo para reanudar: {e}")
        return None


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
    
    # Deshabilitar carga de imágenes para mayor velocidad
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


def extract_visible_products(driver: webdriver.Chrome) -> list[str]:
    # Extraer solo los productos visibles en la página actual
    codes = []
    # Determinar cuántos elementos hay visibles por la cantidad de divs en el listado
    try:
        # XPath genérico para los items de la lista de resultados
        # .../div[2]/div[INDEX]/...
        # Contamos cuántos divs hijos directos hay en el contenedor de resultados
        container_xpath = "/html/body/section/div/div/div[2]/div/div/div/div[2]/div[2]/div"
        items = driver.find_elements(By.XPATH, f"{container_xpath}/div")
        count_visible = len(items)
        
        for i in range(1, count_visible + 1):
            xpath = f"/html/body/section/div/div/div[2]/div/div/div/div[2]/div[2]/div/div[{i}]/div[1]/div/div[2]/div[1]/a/h4"
            try:
                code_elem = driver.find_element(By.XPATH, xpath)
                code_text = code_elem.text.strip()
                if code_text:
                    codes.append(code_text)
            except NoSuchElementException:
                continue
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

def search_specific(driver: webdriver.Chrome, specific_indices: list[int], results: list, timeout: int, only_options: bool = False):
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
        processing_seconds = time.perf_counter() - operation_start
        result_data = {
            "indices": specific_indices,
            "filters": current_values,
            "codes": [],
            "url": driver.current_url,
            "processing_seconds": processing_seconds
        }
        results.append(result_data)
        save_to_csv(result_data, include_codes=False)
        print("✅ Registro específico de opciones completado.")
        return

    try:
        print("  🔎 Click en Buscar...")
        search_button = WebDriverWait(driver, timeout).until(
             EC.element_to_be_clickable((By.XPATH, SEARCH_BUTTON_XPATH))
        )
        click_element(driver, search_button)
        time.sleep(3)
        
        codes = extract_results_with_pagination(driver)
        
        print(f"✅ Búsqueda específica completada. {len(codes)} códigos encontrados.")
        print(f"  Códigos: {codes}")
        
        result_data = {
            "indices": specific_indices,
            "filters": current_values,
            "codes": codes,
            "url": driver.current_url,
            "processing_seconds": time.perf_counter() - operation_start
        }
        results.append(result_data)
        if codes:
            save_to_csv(result_data)
        else:
            print("⚠️ No se encontraron códigos para guardar.")
            
    except Exception as e:
         print(f"❌ Error al ejecutar la búsqueda final: {e}")


def explore_combinations(driver: webdriver.Chrome, timeout: int, depth: int, current_indices: list[int], current_values: list[str], results: list, dev_mode: bool = False, resume_state: dict = None, only_options: bool = False) -> bool:
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
                            results, dev_mode=dev_mode, resume_state=resume_state, only_options=only_options)

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
                    options_batch = []
                    for option_index in iterator_range:
                        option_text = select_obj.options[option_index].text.strip()
                        options_batch.append((option_index, option_text))

                    if options_batch:
                        print(f"{'  ' * depth}Nivel {depth + 1}: último select. Registrando {len(options_batch)} opciones en lote.")
                        save_options_batch(
                            results=results,
                            current_indices=current_indices,
                            current_values=current_values,
                            options_batch=options_batch,
                            current_url=driver.current_url
                        )
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
                            save_to_csv(result_data, include_codes=False)
                            state_dirtied = True
                            continue

                    dirtied = explore_combinations(driver, timeout, depth + 1, current_indices + [i], current_values + [option_text], results, dev_mode=dev_mode, resume_state=resume_state, only_options=only_options)

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
            processing_seconds = 0.0
            operation_start = time.perf_counter()
            print(f"  Registrando combinación: {current_values}")
            result_data = {
                "indices": current_indices.copy(),
                "filters": current_values.copy(),
                "codes": [],
                "url": driver.current_url,
                "processing_seconds": processing_seconds
            }
            result_data["processing_seconds"] = time.perf_counter() - operation_start
            results.append(result_data)
            save_to_csv(result_data, include_codes=False)
            return False

        print(f"  Ejecutando búsqueda para combinación: {current_values}")
        operation_start = time.perf_counter()
        try:
            search_button = WebDriverWait(driver, timeout).until(
                EC.element_to_be_clickable((By.XPATH, SEARCH_BUTTON_XPATH))
            )
            click_element(driver, search_button)
            time.sleep(3) # Esperar resultados
            
            # Extraer resultados (USANDO PAGINACIÓN)
            codes = extract_results_with_pagination(driver)
            
            result_data = {
                "indices": current_indices.copy(),
                "filters": current_values.copy(),
                "codes": codes,
                "url": driver.current_url
            }
            navigate_back_to_form(driver, timeout)

            result_data["processing_seconds"] = time.perf_counter() - operation_start
            results.append(result_data)
            save_to_csv(result_data)
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
    silent: bool = False
) -> dict:
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
            search_specific(driver, specific_indices, combinations_results, timeout, only_options=only_options)
            if only_options:
                result["title"] = f"Registro Específico de Opciones: {specific_indices}"
            else:
                result["title"] = f"Búsqueda Específica: {specific_indices}"
        else:
            explore_combinations(driver, timeout, 0, [], [], combinations_results, dev_mode=dev_mode, resume_state=resume_state, only_options=only_options)
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
        silent=args.silent
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