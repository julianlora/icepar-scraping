from __future__ import annotations

import argparse
import re
from pathlib import Path


ARCHIVO_REGEX = re.compile(r"resultados_productos_ocr_pages_(\d+)_(\d+)\.csv$", re.IGNORECASE)
ORDEN_FALLBACK = 10**12


def obtener_clave_orden(archivo: Path) -> tuple[int, int, str]:
    coincidencia = ARCHIVO_REGEX.fullmatch(archivo.name)
    if not coincidencia:
        return ORDEN_FALLBACK, ORDEN_FALLBACK, archivo.name.lower()
    pagina_inicio, pagina_fin = coincidencia.groups()
    return int(pagina_inicio), int(pagina_fin), archivo.name.lower()


def listar_archivos_csv(directorio: Path, patron: str) -> list[Path]:
    archivos = sorted((archivo for archivo in directorio.glob(patron) if archivo.is_file()), key=obtener_clave_orden)
    if not archivos:
        raise FileNotFoundError(
            f"No se encontraron archivos que coincidan con '{patron}' en: {directorio}"
        )
    return archivos


def normalizar_linea(linea: str) -> str:
    contenido = linea.rstrip("\r\n")
    salto_linea = linea[len(contenido):]
    contenido = contenido.rstrip(";")
    return contenido + salto_linea


def copiar_contenido(origen, destino) -> str:
    ultimo_caracter = "\n"

    for linea in origen:
        linea_normalizada = normalizar_linea(linea)
        destino.write(linea_normalizada)
        if linea_normalizada:
            ultimo_caracter = linea_normalizada[-1]

    return ultimo_caracter


def concatenar_csvs(archivos: list[Path], archivo_salida: Path) -> int:
    encabezado_esperado: str | None = None
    ultimo_caracter_escrito = "\n"
    archivos_procesados = 0

    with archivo_salida.open("w", encoding="utf-8-sig", newline="") as salida:
        for archivo in archivos:
            with archivo.open("r", encoding="utf-8-sig", newline="") as entrada:
                encabezado = entrada.readline()
                if not encabezado:
                    continue

                encabezado_normalizado = normalizar_linea(encabezado).rstrip("\r\n")

                if encabezado_esperado is None:
                    encabezado_esperado = encabezado_normalizado
                    salida.write(encabezado_esperado + "\n")
                    ultimo_caracter_escrito = "\n"
                elif encabezado_normalizado != encabezado_esperado:
                    raise ValueError(f"El encabezado no coincide en el archivo: {archivo.name}")

                if ultimo_caracter_escrito not in {"\n", "\r"}:
                    salida.write("\n")
                    ultimo_caracter_escrito = "\n"

                ultimo_caracter_escrito = copiar_contenido(entrada, salida)
                archivos_procesados += 1

    if encabezado_esperado is None:
        raise ValueError("Los archivos encontrados están vacíos.")

    return archivos_procesados


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Concatena todos los CSV resultados_productos_ocr_pages_* en un único archivo."
    )
    parser.add_argument(
        "--directorio",
        default=".",
        help="Directorio donde se buscarán los archivos CSV.",
    )
    parser.add_argument(
        "--patron",
        default="resultados_productos_ocr_pages_*.csv",
        help="Patrón glob de los archivos a concatenar.",
    )
    parser.add_argument(
        "--salida",
        default="resultados_productos_ocr_pages_concatenado.csv",
        help="Ruta del archivo CSV de salida.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    directorio = Path(args.directorio).resolve()
    archivo_salida = Path(args.salida).resolve()

    if not directorio.exists():
        raise FileNotFoundError(f"No se encontró el directorio: {directorio}")

    archivos = listar_archivos_csv(directorio=directorio, patron=args.patron)
    archivos = [archivo for archivo in archivos if archivo.resolve() != archivo_salida]

    if not archivos:
        raise FileNotFoundError("No quedaron archivos para concatenar luego de excluir el archivo de salida.")

    archivos_procesados = concatenar_csvs(archivos=archivos, archivo_salida=archivo_salida)

    print(f"Archivo generado: {archivo_salida}")
    print(f"Archivos concatenados: {archivos_procesados}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())