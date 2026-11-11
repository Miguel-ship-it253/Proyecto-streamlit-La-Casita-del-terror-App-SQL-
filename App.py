"""Casa del Terror: aplicacion de reservas (Streamlit + Oracle) en un solo archivo.
Proyecto final de Introduccion a BI (EAFIT).

Ejecutar desde esta carpeta:   streamlit run App.py

Contenido del archivo, en este orden:
  1. Importaciones
  2. Acceso a Oracle (pool, consultar, transaccion)
  3. Reglas del negocio N01-N17 (las pantallas nunca escriben datos: llaman a estas funciones)
  4. Ilustraciones (SVG generado por codigo)
  5. Piezas visuales (menu, tarjetas, formato) y control de acceso por rol
  6. Estilos (CSS del tema de terror)
  7. Pantallas del cliente
  8. Pantallas del administrador
  9. Arranque de la aplicacion
"""
# ======================================================================================
# 1. IMPORTACIONES
# ======================================================================================
import base64
import hmac
import html
import io
import os
import random
import re
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP

import altair as alt
import oracledb
import pandas as pd
import segno
import streamlit as st



# ======================================================================================
# 2. ACCESO A ORACLE (estilo de la clase: consultar + transaccion)
# ======================================================================================
class ReglaError(Exception):
    """Una regla del negocio impide la operacion (se muestra tal cual al usuario)."""

MAX_FILAS = 1000   # nunca se descargan tablas completas; Oracle agrupa y filtra


def _credenciales():
    # Para pruebas automaticas se pueden usar variables de entorno; la app usa secrets.toml
    if os.environ.get("ORACLE_USER"):
        return os.environ["ORACLE_USER"], os.environ["ORACLE_PASSWORD"], os.environ["ORACLE_DSN"]
    c = st.secrets["oracle"]
    return c["user"], c["password"], c["dsn"]


@st.cache_resource(show_spinner=False)
def get_pool():
    user, password, dsn = _credenciales()
    return oracledb.create_pool(user=user, password=password, dsn=dsn, min=1, max=8, increment=1)


def _minusculas(cur):
    cur.rowfactory = None
    return [d[0].lower() for d in cur.description]


def consultar(sql, **params):
    """SELECT con parametros con nombre -> DataFrame (columnas en minuscula)."""
    with get_pool().acquire() as con:
        with con.cursor() as cur:
            cur.execute(sql, params)
            cols = _minusculas(cur)
            filas = cur.fetchmany(MAX_FILAS)
    return pd.DataFrame(filas, columns=cols)


def fila(sql, **params):
    """Primera fila como diccionario (o None)."""
    df = consultar(sql, **params)
    return None if df.empty else {k: _py(v) for k, v in df.iloc[0].to_dict().items()}


def escalar(sql, **params):
    f = fila(sql, **params)
    return None if f is None else next(iter(f.values()))


def _py(v):
    """Convierte tipos de pandas/numpy a tipos nativos (None en lugar de NaN/NaT)."""
    if v is None or (not isinstance(v, str) and pd.isna(v)):
        return None
    if hasattr(v, "item"):
        v = v.item()
    return v


class Tx:
    """Operaciones dentro de una misma transaccion (comparten conexion)."""

    def __init__(self, con):
        self.con = con

    def ejecutar(self, sql, **params):
        with self.con.cursor() as cur:
            cur.execute(sql, params)
            return cur.rowcount

    def fila(self, sql, **params):
        with self.con.cursor() as cur:
            cur.execute(sql, params)
            cols = [d[0].lower() for d in cur.description]
            r = cur.fetchone()
        return None if r is None else dict(zip(cols, r))

    def escalar(self, sql, **params):
        f = self.fila(sql, **params)
        return None if f is None else next(iter(f.values()))

    def insertar(self, sql, columna_id, **params):
        """INSERT ... RETURNING <id> INTO :nuevo_id -> id generado."""
        with self.con.cursor() as cur:
            nuevo = cur.var(int)
            cur.execute(f"{sql} RETURNING {columna_id} INTO :nuevo_id", {**params, "nuevo_id": nuevo})
            return int(nuevo.getvalue()[0])


@contextmanager
def transaccion():
    with get_pool().acquire() as con:
        try:
            yield Tx(con)
            con.commit()
        except BaseException:
            con.rollback()
            raise


# ------------------------------------------------------------------ mensajes (flash)
def flash(mensaje, tipo="success"):
    st.session_state.setdefault("_flash", []).append((tipo, mensaje))


def mostrar_flash():
    for tipo, msg in st.session_state.pop("_flash", []):
        {"success": st.success, "error": st.error, "warning": st.warning, "info": st.info}[tipo](msg)


# ======================================================================================
# 3. REGLAS DEL NEGOCIO (N01-N17)
# ======================================================================================
# ------------------------------------------------------------------ parametros del negocio
MAX_PERSONAS_COMPRA = 15
HORAS_ANTICIPACION_RESERVA = 24      # N10
HORAS_CANCELACION_CLIENTE = 48       # N12
MINUTOS_ANTES_PARA_INICIAR = 60      # el admin puede iniciar una sesion desde 1 h antes

ASISTIO, NO_ASISTIO = "ASISTIÓ", "NO ASISTIÓ"
TEMATICAS = ["ZOMBIES", "FANTASMAS", "HOSPITAL ABANDONADO", "PSICOLÓGICO", "SLASHER", "OCULTISMO", "OTRA"]
MOTIVOS = ["NINGUNO", "CUMPLEAÑOS", "DESPEDIDA", "EMPRESARIAL", "ANIVERSARIO"]
INTENSIDADES = ["SUAVE", "MEDIA", "EXTREMA"]
METODOS_WEB = ["TARJETA", "PSE", "TRANSFERENCIA"]
METODOS_TAQUILLA = ["EFECTIVO", "TARJETA", "TRANSFERENCIA", "PSE"]
TIPOS_DOC = ["CC", "CE", "TI", "PASAPORTE"]
RE_CORREO = re.compile(r"^[^@ ]+@[^@ ]+\.[^@ ]+$")
RE_HORA = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
RE_DOC = re.compile(r"^[A-Za-z0-9]{4,20}$")

# Momento de inicio de una sesion (fecha + hora 'HH:MM'); el reloj es SYSDATE de Oracle.
def inicio_sql(a="s"):
    return f"TO_DATE(TO_CHAR({a}.s_fecha,'YYYY-MM-DD')||' '||{a}.s_hora_inicio,'YYYY-MM-DD HH24:MI')"


# ------------------------------------------------------------------ utilidades
def _dec(x):
    return Decimal(str(x))


def calcular_precio(porcentaje, personas, valor_unitario):
    """N06: recargo = ROUND(% / 100 x personas x unitario, 2); total = personas x unitario + recargo."""
    unit = _dec(valor_unitario)
    recargo = (_dec(porcentaje) / 100 * int(personas) * unit).quantize(Decimal("0.01"), ROUND_HALF_UP)
    return recargo, int(personas) * unit + recargo


def _entero(v, campo="El numero de personas"):
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ReglaError(f"{campo} debe ser un número entero.")
    if n != v and not isinstance(v, str):
        raise ReglaError(f"{campo} debe ser un número entero.")
    return n


def _limpiar(texto, maximo, campo, obligatorio=False):
    t = (texto or "").strip()
    if obligatorio and not t:
        raise ReglaError(f"{campo} es obligatorio.")
    if len(t) > maximo:
        raise ReglaError(f"{campo} no puede pasar de {maximo} caracteres.")
    return t or None


def ahora():
    return escalar("SELECT SYSDATE FROM dual")


def _integridad(e):
    msg = str(e)
    if "UQ_CLIENTES_DOCUMENTO" in msg:
        return ReglaError("Ya existe una cuenta con ese número de documento.")
    if "UQ_CLIENTES_CORREO" in msg:
        return ReglaError("Ya existe una cuenta con ese correo.")
    if "UQ_EXPERIENCIAS_NOMBRE" in msg:
        return ReglaError("Ya existe una experiencia con ese nombre.")
    if "UQ_SESIONES_FECHA_HORA_ZONA" in msg:
        return ReglaError("Esa sala ya tiene una sesión a esa fecha y hora.")
    return ReglaError("La base de datos rechazó el cambio porque rompe una restricción de integridad.")


# ================================================================== CLIENTES
def autenticar_cliente(documento, correo):
    """Acceso SIMULADO: documento + correo contra CLIENTE (no hay tabla de usuarios ni contrasenas)."""
    documento, correo = (documento or "").strip(), (correo or "").strip().lower()
    if not documento or not correo:
        raise ReglaError("Escribe tu número de documento y tu correo.")
    c = fila("""SELECT c_id_clientes AS id, c_nombre AS nombre, c_apellido AS apellido, c_estado AS estado
                FROM cliente WHERE c_numero_documento = :d AND LOWER(c_correo) = :c AND c_numero_documento <> '0'""",
             d=documento, c=correo)
    if not c:
        raise ReglaError("No encontramos una cuenta con ese documento y correo.")
    if c["estado"] != "ACTIVO":
        raise ReglaError("Tu cuenta está inactiva. Comunícate con la administración.")
    return c


def registrar_cliente(tipo_doc, documento, nombre, apellido, correo, telefono, nacimiento):
    if tipo_doc not in TIPOS_DOC:
        raise ReglaError("Tipo de documento no válido.")
    documento = (documento or "").strip()
    if not RE_DOC.match(documento) or documento == "0":
        raise ReglaError("El documento debe tener entre 4 y 20 letras o números, sin espacios.")
    nombre = _limpiar(nombre, 100, "El nombre", True)
    apellido = _limpiar(apellido, 100, "El apellido", True)
    correo = _limpiar(correo, 150, "El correo", True).lower()
    if not RE_CORREO.match(correo):
        raise ReglaError("El correo no tiene un formato válido (ejemplo: nombre@dominio.com).")
    telefono = _limpiar(telefono, 20, "El teléfono")
    if telefono and not re.match(r"^[0-9+ ]{7,20}$", telefono):
        raise ReglaError("El teléfono solo puede tener números, espacios y el signo +.")
    if not isinstance(nacimiento, date):
        raise ReglaError("La fecha de nacimiento es obligatoria (se usa para validar la edad mínima).")
    if nacimiento > date.today() or nacimiento.year < 1900:
        raise ReglaError("La fecha de nacimiento no es válida.")
    try:
        with transaccion() as tx:
            return tx.insertar("""INSERT INTO cliente (c_tipo_documento, c_numero_documento, c_nombre, c_apellido,
                                  c_correo, c_telefono, c_fecha_nacimiento, c_estado)
                                  VALUES (:t, :d, :n, :a, :c, :tel, :f, 'ACTIVO')""", "c_id_clientes",
                               t=tipo_doc, d=documento, n=nombre, a=apellido, c=correo, tel=telefono, f=nacimiento)
    except oracledb.IntegrityError as e:
        raise _integridad(e)


def actualizar_datos_cliente(cliente_id, correo, telefono):
    """N17: el cliente solo modifica lo suyo (contacto; documento y nacimiento no se tocan)."""
    correo = _limpiar(correo, 150, "El correo", True).lower()
    if not RE_CORREO.match(correo):
        raise ReglaError("El correo no tiene un formato válido.")
    telefono = _limpiar(telefono, 20, "El teléfono")
    if telefono and not re.match(r"^[0-9+ ]{7,20}$", telefono):
        raise ReglaError("El teléfono solo puede tener números, espacios y el signo +.")
    try:
        with transaccion() as tx:
            n = tx.ejecutar("UPDATE cliente SET c_correo=:c, c_telefono=:t WHERE c_id_clientes=:i AND c_numero_documento<>'0'",
                            c=correo, t=telefono, i=cliente_id)
            if n == 0:
                raise ReglaError("No se encontró tu cuenta.")
    except oracledb.IntegrityError as e:
        raise _integridad(e)


def cambiar_estado_cliente(cliente_id, nuevo):
    """N16: el VISITANTE GENERAL no se toca; un cliente con ventas activas en sesiones programadas no se inactiva."""
    if nuevo not in ("ACTIVO", "INACTIVO"):
        raise ReglaError("Estado no válido.")
    with transaccion() as tx:
        c = tx.fila("SELECT c_numero_documento AS doc, c_estado AS estado FROM cliente WHERE c_id_clientes=:i FOR UPDATE", i=cliente_id)
        if not c:
            raise ReglaError("El cliente no existe.")
        if c["doc"] == "0":
            raise ReglaError("El VISITANTE GENERAL es parte de la taquilla y no se puede modificar.")
        if nuevo == "INACTIVO":
            n = tx.escalar(f"""SELECT COUNT(*) FROM venta v JOIN sesion s ON s.s_id_sesiones = v.v_id_sesiones
                               WHERE v.v_id_clientes=:i AND v.v_estado<>'CANCELADA' AND s.s_estado='PROGRAMADA'""", i=cliente_id)
            if n:
                raise ReglaError(f"No se puede inactivar: tiene {n} venta(s) activa(s) en sesiones programadas. Cancélalas primero.")
        tx.ejecutar("UPDATE cliente SET c_estado=:e WHERE c_id_clientes=:i", e=nuevo, i=cliente_id)


# ================================================================== VENTAS
def comprar(cliente_id, sesion_id, tipo_id, personas, metodo, *, intensidad=None, actores=False, efectos=False,
            motivo=None, notas=None, metodos_validos=METODOS_WEB):
    """Crea una venta CONFIRMADA (pago simulado) y descuenta cupos. Aplica N01-N11."""
    personas = _entero(personas)
    if metodo not in metodos_validos:
        raise ReglaError("Método de pago no válido.")
    with transaccion() as tx:
        # 1) bloquear la sesion: serializa compras simultaneas de la misma sesion
        s = tx.fila(f"""SELECT s.s_id_sesiones AS sid, s.s_id_experiencias AS eid, s.s_estado AS sest,
                               s.s_cupos_disponibles AS cupos, s.s_fecha AS fecha, ({inicio_sql()} - SYSDATE) * 24 AS horas,
                               e.ex_nombre AS exp, e.ex_estado AS eest, e.ex_capacidad_maxima AS cap,
                               e.ex_edad_minima AS edad_min, e.ex_precio_base AS precio
                        FROM sesion s JOIN experiencia e ON e.ex_id_experiencias = s.s_id_experiencias
                        WHERE s.s_id_sesiones = :s FOR UPDATE OF s.s_cupos_disponibles""", s=sesion_id)
        if not s:
            raise ReglaError("La sesión no existe.")
        t = tx.fila("""SELECT te_id_tipos_experiencia AS id, te_nombre AS nombre, te_permite_personalizacion AS pers,
                              te_requiere_reserva AS reserva, te_recargo_porcentaje AS pct
                       FROM tipo_experiencia WHERE te_id_tipos_experiencia = :t""", t=tipo_id)
        if not t:
            raise ReglaError("El tipo de entrada no existe.")
        c = tx.fila("""SELECT c_id_clientes AS id, c_estado AS estado, c_numero_documento AS doc,
                              MONTHS_BETWEEN(:f, c_fecha_nacimiento) / 12 AS edad
                       FROM cliente WHERE c_id_clientes = :c""", f=s["fecha"], c=cliente_id)
        if not c:
            raise ReglaError("El cliente no existe.")
        visitante = c["doc"] == "0"

        # N02 cliente activo
        if c["estado"] != "ACTIVO":
            raise ReglaError("Tu cuenta está inactiva y no puede comprar.")
        # N01 experiencia activa, sesion programada y no iniciada
        if s["eest"] != "ACTIVA":
            raise ReglaError("Esta experiencia no está disponible en este momento.")
        if s["sest"] != "PROGRAMADA":
            raise ReglaError("Esta sesión ya no está disponible para la venta.")
        if s["horas"] <= 0:
            raise ReglaError("Esta sesión ya empezó; no se pueden registrar ventas.")
        # N09 la reserva personalizada es solo para clientes registrados
        if t["reserva"] == "SI" and visitante:
            raise ReglaError("La reserva personalizada es solo para clientes registrados.")
        # N10 reserva con 24 horas de anticipacion
        if t["reserva"] == "SI" and s["horas"] < HORAS_ANTICIPACION_RESERVA:
            raise ReglaError(f"La reserva personalizada se hace con al menos {HORAS_ANTICIPACION_RESERVA} horas de anticipación.")
        # N03 personas: >=1, <= maximo por compra, <= capacidad, <= cupos
        if personas < 1:
            raise ReglaError("Debe haber al menos una persona.")
        if personas > MAX_PERSONAS_COMPRA:
            raise ReglaError(f"Máximo {MAX_PERSONAS_COMPRA} personas por compra.")
        if personas > s["cap"]:
            raise ReglaError(f"Esta experiencia admite como máximo {int(s['cap'])} personas.")
        if personas > s["cupos"]:
            raise ReglaError("No hay cupos suficientes en esta sesión." if s["cupos"] > 0 else "Esta sesión está agotada.")
        # N05 edad minima en la fecha de la sesion (no aplica al visitante general de la taquilla)
        if not visitante:
            if c["edad"] is None:
                raise ReglaError("Tu cuenta no tiene fecha de nacimiento; no se puede validar la edad mínima.")
            if c["edad"] < s["edad_min"]:
                raise ReglaError(f"Esta experiencia es para mayores de {int(s['edad_min'])} años (a la fecha de la sesión).")
        # N08 una reserva personalizada exige sesion vacia; una sesion con reserva personalizada es privada
        act = tx.fila("""SELECT COUNT(*) AS n, NVL(SUM(CASE WHEN t.te_permite_personalizacion='SI' THEN 1 ELSE 0 END),0) AS priv
                         FROM venta v JOIN tipo_experiencia t ON t.te_id_tipos_experiencia = v.v_id_tipos_experiencia
                         WHERE v.v_id_sesiones = :s AND v.v_estado <> 'CANCELADA'""", s=sesion_id)
        if act["priv"] > 0:
            raise ReglaError("Esta sesión está reservada de forma privada y no admite más ventas.")
        if t["reserva"] == "SI" and act["n"] > 0:
            raise ReglaError("Para una reserva personalizada la sesión debe estar libre (ya tiene entradas vendidas).")

        # N04 personalizacion solo si el tipo la permite
        if t["pers"] == "SI":
            if intensidad not in INTENSIDADES:
                raise ReglaError("Elige el nivel de intensidad.")
            motivo = motivo or "NINGUNO"
            if motivo not in MOTIVOS:
                raise ReglaError("Motivo de celebración no válido.")
            notas = _limpiar(notas, 500, "Las notas")
            actores, efectos = "SI" if actores else "NO", "SI" if efectos else "NO"
        else:
            if intensidad or actores or efectos or (notas or "").strip() or (motivo not in (None, "NINGUNO")):
                raise ReglaError("Este tipo de entrada no admite personalización.")
            intensidad, actores, efectos, motivo, notas = None, "NO", "NO", None, None

        # N06 precio calculado por la app: el valor unitario se copia de la experiencia
        unit = _dec(s["precio"])
        recargo, total = calcular_precio(t["pct"], personas, unit)

        # N03 descuento atomico de cupos (si otro se llevo los cupos, 0 filas -> ROLLBACK)
        n = tx.ejecutar("""UPDATE sesion SET s_cupos_disponibles = s_cupos_disponibles - :n
                           WHERE s_id_sesiones = :s AND s_cupos_disponibles >= :n AND s_estado = 'PROGRAMADA'""",
                        n=personas, s=sesion_id)
        if n == 0:
            raise ReglaError("Otra persona acaba de tomar esos cupos. Elige otra sesión o menos personas.")
        # N07 la experiencia de la venta es la de su sesion; N11 nace CONFIRMADA
        vid = tx.insertar("""INSERT INTO venta (v_id_clientes, v_id_experiencias, v_id_sesiones, v_id_tipos_experiencia,
                                v_fecha_registro, v_cantidad_personas, v_valor_unitario, v_recargo, v_valor_total,
                                v_nivel_intensidad, v_actores_en_vivo, v_efectos_especiales, v_motivo_celebracion,
                                v_notas, v_metodo_pago, v_estado)
                             VALUES (:c, :e, :s, :t, CAST(SYSDATE AS TIMESTAMP), :n, :u, :r, :tot, :i, :a, :ef, :m,
                                     :no, :p, 'CONFIRMADA')""", "v_id_ventas",
                          c=cliente_id, e=s["eid"], s=sesion_id, t=tipo_id, n=personas, u=unit, r=recargo, tot=total,
                          i=intensidad, a=actores, ef=efectos, m=motivo, no=notas, p=metodo)
    return {"id": vid, "total": total, "recargo": recargo, "experiencia": s["exp"]}


def cancelar_venta(venta_id, *, cliente_id=None):
    """N12 (cliente: solo su reserva personalizada, 48 h antes) y N13 (admin: sesion no finalizada).
    cliente_id=None significa administrador. Devuelve los cupos a la sesion."""
    with transaccion() as tx:
        v0 = tx.fila("SELECT v_id_sesiones AS sid FROM venta WHERE v_id_ventas = :v", v=venta_id)
        if not v0:
            raise ReglaError("La venta no existe.")
        s = tx.fila(f"SELECT s.s_estado AS sest, ({inicio_sql()} - SYSDATE) * 24 AS horas FROM sesion s "
                    "WHERE s.s_id_sesiones = :s FOR UPDATE", s=v0["sid"])
        v = tx.fila("""SELECT v.v_id_clientes AS cid, v.v_estado AS estado, v.v_cantidad_personas AS n, t.te_requiere_reserva AS reserva
                       FROM venta v JOIN tipo_experiencia t ON t.te_id_tipos_experiencia = v.v_id_tipos_experiencia
                       WHERE v.v_id_ventas = :v FOR UPDATE OF v.v_estado""", v=venta_id)
        if cliente_id is not None and v["cid"] != cliente_id:          # N17
            raise ReglaError("No encontramos esa reserva en tu cuenta.")
        if v["estado"] not in ("CONFIRMADA", "PENDIENTE"):             # N14
            raise ReglaError(f"Una venta en estado {v['estado']} ya no se puede cancelar.")
        if cliente_id is not None:
            if v["reserva"] != "SI":
                raise ReglaError("Las entradas generales no admiten cambios ni cancelaciones.")
            if s["sest"] != "PROGRAMADA" or s["horas"] < HORAS_CANCELACION_CLIENTE:
                raise ReglaError(f"Solo puedes cancelar hasta {HORAS_CANCELACION_CLIENTE} horas antes de la sesión.")
        elif s["sest"] == "FINALIZADA":
            raise ReglaError("La sesión ya finalizó; la venta no se puede cancelar.")
        tx.ejecutar("UPDATE venta SET v_estado='CANCELADA' WHERE v_id_ventas=:v", v=venta_id)
        tx.ejecutar("UPDATE sesion SET s_cupos_disponibles = s_cupos_disponibles + :n WHERE s_id_sesiones=:s",
                    n=v["n"], s=v0["sid"])


def marcar_asistencia(venta_id, asistio):
    """N14: asistencia solo en sesiones EN CURSO o FINALIZADA y sobre ventas CONFIRMADAS."""
    with transaccion() as tx:
        v0 = tx.fila("SELECT v_id_sesiones AS sid FROM venta WHERE v_id_ventas = :v", v=venta_id)
        if not v0:
            raise ReglaError("La venta no existe.")
        s = tx.fila("SELECT s_estado AS sest FROM sesion WHERE s_id_sesiones=:s FOR UPDATE", s=v0["sid"])
        v = tx.fila("SELECT v_estado AS estado FROM venta WHERE v_id_ventas=:v FOR UPDATE", v=venta_id)
        if s["sest"] not in ("EN CURSO", "FINALIZADA"):
            raise ReglaError("La asistencia solo se registra cuando la sesión está EN CURSO.")
        if v["estado"] not in ("CONFIRMADA", "PENDIENTE"):
            raise ReglaError(f"La venta está en estado {v['estado']}; ya no admite asistencia.")
        tx.ejecutar("UPDATE venta SET v_estado=:e WHERE v_id_ventas=:v", e=ASISTIO if asistio else NO_ASISTIO, v=venta_id)


# ================================================================== SESIONES (admin)
def crear_sesion(exp_id, fecha, hora, zona, cupos):
    hora = (hora or "").strip()
    if not RE_HORA.match(hora):
        raise ReglaError("La hora debe tener formato HH:MM (24 horas).")
    zona = _limpiar(zona, 50, "La sala", True)
    cupos = _entero(cupos, "Los cupos")
    if not isinstance(fecha, date):
        raise ReglaError("Elige la fecha de la sesión.")
    try:
        with transaccion() as tx:
            e = tx.fila("SELECT ex_estado AS estado, ex_capacidad_maxima AS cap FROM experiencia WHERE ex_id_experiencias=:e FOR UPDATE", e=exp_id)
            if not e:
                raise ReglaError("La experiencia no existe.")
            if e["estado"] != "ACTIVA":
                raise ReglaError("Solo se programan sesiones de experiencias ACTIVAS.")
            if cupos < 1 or cupos > e["cap"]:
                raise ReglaError(f"Los cupos deben estar entre 1 y {int(e['cap'])} (capacidad de la experiencia).")
            futuro = tx.escalar("SELECT CASE WHEN TO_DATE(:f||' '||:h,'YYYY-MM-DD HH24:MI') > SYSDATE THEN 1 ELSE 0 END FROM dual",
                                f=fecha.isoformat(), h=hora)
            if not futuro:
                raise ReglaError("La sesión debe programarse en el futuro.")
            return tx.insertar("""INSERT INTO sesion (s_id_experiencias, s_fecha, s_hora_inicio, s_zona, s_cupos_totales,
                                  s_cupos_disponibles, s_estado) VALUES (:e, :f, :h, :z, :c, :c, 'PROGRAMADA')""",
                               "s_id_sesiones", e=exp_id, f=fecha, h=hora, z=zona, c=cupos)
    except oracledb.IntegrityError as ex:
        raise _integridad(ex)


def cambiar_cupos_sesion(sesion_id, nuevos_totales):
    """N15: cupos totales <= capacidad y >= lo ya vendido (solo sesiones PROGRAMADAS)."""
    nuevos = _entero(nuevos_totales, "Los cupos")
    with transaccion() as tx:
        s = tx.fila("""SELECT s.s_estado AS est, s.s_cupos_totales AS tot, s.s_cupos_disponibles AS disp, e.ex_capacidad_maxima AS cap
                       FROM sesion s JOIN experiencia e ON e.ex_id_experiencias=s.s_id_experiencias
                       WHERE s.s_id_sesiones=:s FOR UPDATE OF s.s_cupos_totales""", s=sesion_id)
        if not s:
            raise ReglaError("La sesión no existe.")
        if s["est"] != "PROGRAMADA":
            raise ReglaError("Solo se editan sesiones PROGRAMADAS.")
        vendido = s["tot"] - s["disp"]
        if nuevos < max(1, vendido):
            raise ReglaError(f"Los cupos no pueden ser menores a lo ya vendido ({int(vendido)} personas).")
        if nuevos > s["cap"]:
            raise ReglaError(f"Los cupos no pueden superar la capacidad de la experiencia ({int(s['cap'])}).")
        tx.ejecutar("UPDATE sesion SET s_cupos_totales=:n, s_cupos_disponibles=:n - :v WHERE s_id_sesiones=:s", n=nuevos, v=vendido, s=sesion_id)


def iniciar_sesion(sesion_id):
    with transaccion() as tx:
        s = tx.fila(f"SELECT s.s_estado AS est, ({inicio_sql()} - SYSDATE) * 1440 AS minutos FROM sesion s WHERE s.s_id_sesiones=:s FOR UPDATE", s=sesion_id)
        if not s:
            raise ReglaError("La sesión no existe.")
        if s["est"] != "PROGRAMADA":
            raise ReglaError("Solo se inicia una sesión PROGRAMADA.")
        if s["minutos"] > MINUTOS_ANTES_PARA_INICIAR:
            raise ReglaError(f"Aún es temprano: la sesión se puede iniciar desde {MINUTOS_ANTES_PARA_INICIAR} minutos antes de su hora.")
        tx.ejecutar("UPDATE sesion SET s_estado='EN CURSO' WHERE s_id_sesiones=:s", s=sesion_id)


def finalizar_sesion(sesion_id):
    """N15: para finalizar hay que resolver la asistencia de todas las ventas."""
    with transaccion() as tx:
        s = tx.fila("SELECT s_estado AS est FROM sesion WHERE s_id_sesiones=:s FOR UPDATE", s=sesion_id)
        if not s:
            raise ReglaError("La sesión no existe.")
        if s["est"] != "EN CURSO":
            raise ReglaError("Solo se finaliza una sesión EN CURSO.")
        n = tx.escalar("SELECT COUNT(*) FROM venta WHERE v_id_sesiones=:s AND v_estado IN ('PENDIENTE','CONFIRMADA')", s=sesion_id)
        if n:
            raise ReglaError(f"Faltan {n} venta(s) por marcar asistencia (asistió / no asistió).")
        tx.ejecutar("UPDATE sesion SET s_estado='FINALIZADA' WHERE s_id_sesiones=:s", s=sesion_id)


def cancelar_sesion(sesion_id):
    """N15: cancelar una sesion cancela sus ventas activas y devuelve los cupos."""
    with transaccion() as tx:
        s = tx.fila("SELECT s_estado AS est, s_cupos_totales AS tot FROM sesion WHERE s_id_sesiones=:s FOR UPDATE", s=sesion_id)
        if not s:
            raise ReglaError("La sesión no existe.")
        if s["est"] != "PROGRAMADA":
            raise ReglaError("Solo se cancela una sesión PROGRAMADA.")
        n = tx.ejecutar("UPDATE venta SET v_estado='CANCELADA' WHERE v_id_sesiones=:s AND v_estado <> 'CANCELADA'", s=sesion_id)
        tx.ejecutar("UPDATE sesion SET s_estado='CANCELADA', s_cupos_disponibles=s_cupos_totales WHERE s_id_sesiones=:s", s=sesion_id)
        return n


# ================================================================== EXPERIENCIAS (admin)
def _validar_experiencia(nombre, tematica, descripcion, duracion, precio, capacidad, edad, estado):
    nombre = _limpiar(nombre, 100, "El nombre", True)
    if tematica not in TEMATICAS:
        raise ReglaError("Temática no válida.")
    descripcion = _limpiar(descripcion, 500, "La descripción")
    duracion, capacidad, edad = _entero(duracion, "La duracion"), _entero(capacidad, "La capacidad"), _entero(edad, "La edad minima")
    if duracion <= 0:
        raise ReglaError("La duración debe ser mayor a 0 minutos.")
    if capacidad <= 0:
        raise ReglaError("La capacidad debe ser mayor a 0.")
    if not 0 <= edad <= 99:
        raise ReglaError("La edad mínima debe estar entre 0 y 99.")
    try:
        precio = _dec(precio)
    except Exception:
        raise ReglaError("El precio no es válido.")
    if precio < 0 or precio > Decimal("99999999.99") or precio != precio.quantize(Decimal("0.01")):
        raise ReglaError("El precio debe ser positivo y tener máximo 2 decimales.")
    if estado not in ("ACTIVA", "INACTIVA", "MANTENIMIENTO"):
        raise ReglaError("Estado no válido.")
    return nombre, tematica, descripcion, duracion, precio, capacidad, edad, estado


def crear_experiencia(nombre, tematica, descripcion, duracion, precio, capacidad, edad, estado="ACTIVA"):
    v = _validar_experiencia(nombre, tematica, descripcion, duracion, precio, capacidad, edad, estado)
    try:
        with transaccion() as tx:
            return tx.insertar("""INSERT INTO experiencia (ex_nombre, ex_tematica, ex_descripcion, ex_duracion_minutos,
                                  ex_precio_base, ex_capacidad_maxima, ex_edad_minima, ex_estado)
                                  VALUES (:n, :t, :d, :du, :p, :c, :e, :es)""", "ex_id_experiencias",
                               n=v[0], t=v[1], d=v[2], du=v[3], p=v[4], c=v[5], e=v[6], es=v[7])
    except oracledb.IntegrityError as e:
        raise _integridad(e)


def editar_experiencia(exp_id, nombre, tematica, descripcion, duracion, precio, capacidad, edad, estado):
    v = _validar_experiencia(nombre, tematica, descripcion, duracion, precio, capacidad, edad, estado)
    try:
        with transaccion() as tx:
            if not tx.fila("SELECT 1 AS x FROM experiencia WHERE ex_id_experiencias=:e FOR UPDATE", e=exp_id):
                raise ReglaError("La experiencia no existe.")
            # R18 / R01: la capacidad no puede quedar por debajo de lo ya programado o vendido
            mx = tx.escalar("SELECT NVL(MAX(s_cupos_totales),0) FROM sesion WHERE s_id_experiencias=:e", e=exp_id)
            mp = tx.escalar("SELECT NVL(MAX(v_cantidad_personas),0) FROM venta WHERE v_id_experiencias=:e", e=exp_id)
            if v[5] < max(mx, mp):
                raise ReglaError(f"La capacidad no puede ser menor a {int(max(mx, mp))}: ya hay sesiones o ventas con ese tamaño de grupo.")
            # R05: subir la edad minima no puede dejar ventas historicas por debajo de la nueva edad
            bad = tx.escalar("""SELECT COUNT(*) FROM venta v JOIN cliente c ON c.c_id_clientes=v.v_id_clientes
                                JOIN sesion s ON s.s_id_sesiones=v.v_id_sesiones
                                WHERE v.v_id_experiencias=:e AND c.c_numero_documento<>'0'
                                  AND MONTHS_BETWEEN(s.s_fecha, c.c_fecha_nacimiento)/12 < :ed""", e=exp_id, ed=v[6])
            if bad:
                raise ReglaError(f"No se puede subir la edad mínima a {v[6]}: {bad} venta(s) existentes son de personas más jóvenes.")
            # R19: una experiencia no activa no puede tener sesiones programadas
            if v[7] != "ACTIVA":
                prog = tx.escalar("SELECT COUNT(*) FROM sesion WHERE s_id_experiencias=:e AND s_estado='PROGRAMADA'", e=exp_id)
                if prog:
                    raise ReglaError(f"No se puede dejar en {v[7]}: tiene {prog} sesión(es) programada(s). Cancélalas primero.")
            tx.ejecutar("""UPDATE experiencia SET ex_nombre=:n, ex_tematica=:t, ex_descripcion=:d, ex_duracion_minutos=:du,
                           ex_precio_base=:p, ex_capacidad_maxima=:c, ex_edad_minima=:e, ex_estado=:es
                           WHERE ex_id_experiencias=:i""", n=v[0], t=v[1], d=v[2], du=v[3], p=v[4], c=v[5], e=v[6], es=v[7], i=exp_id)
    except oracledb.IntegrityError as e:
        raise _integridad(e)


def eliminar_experiencia(exp_id):
    """N16: solo se borra una experiencia sin sesiones (si tiene historia, se inactiva)."""
    with transaccion() as tx:
        tx.fila("SELECT 1 AS x FROM experiencia WHERE ex_id_experiencias=:e FOR UPDATE", e=exp_id)
        if tx.escalar("SELECT COUNT(*) FROM sesion WHERE s_id_experiencias=:e", e=exp_id):
            raise ReglaError("Tiene sesiones registradas, por eso no se borra. Ponla INACTIVA para sacarla de la cartelera.")
        tx.ejecutar("DELETE FROM experiencia WHERE ex_id_experiencias=:e", e=exp_id)


# ================================================================== CONSULTAS COMPARTIDAS
def sesiones_disponibles(exp_id, tipo_requiere_reserva):
    """Sesiones que el cliente puede elegir (misma logica que valida comprar())."""
    horas_min = HORAS_ANTICIPACION_RESERVA if tipo_requiere_reserva else 0
    df = consultar(f"""
        SELECT s.s_id_sesiones AS id, s.s_fecha AS fecha, s.s_hora_inicio AS hora, s.s_zona AS zona,
               s.s_cupos_totales AS totales, s.s_cupos_disponibles AS cupos,
               (SELECT COUNT(*) FROM venta v WHERE v.v_id_sesiones = s.s_id_sesiones AND v.v_estado <> 'CANCELADA') AS ventas,
               (SELECT COUNT(*) FROM venta v JOIN tipo_experiencia t ON t.te_id_tipos_experiencia = v.v_id_tipos_experiencia
                 WHERE v.v_id_sesiones = s.s_id_sesiones AND v.v_estado <> 'CANCELADA' AND t.te_permite_personalizacion = 'SI') AS privadas
        FROM sesion s
        WHERE s.s_id_experiencias = :e AND s.s_estado = 'PROGRAMADA'
          AND ({inicio_sql()} - SYSDATE) * 24 > :h
        ORDER BY s.s_fecha, s.s_hora_inicio""", e=exp_id, h=horas_min)
    if df.empty:
        return df
    df = df[df["privadas"] == 0]
    df = df[df["ventas"] == 0] if tipo_requiere_reserva else df[df["cupos"] > 0]
    return df.reset_index(drop=True)


# ======================================================================================
# 4. ILUSTRACIONES (SVG generado por codigo, sin imagenes externas)
# ======================================================================================
def uri(svg):
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()

def _defs(uid, c1, c2, glow):
    return f"""<defs>
<linearGradient id="sky{uid}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{c1}"/><stop offset="1" stop-color="{c2}"/></linearGradient>
<radialGradient id="mg{uid}"><stop offset="0" stop-color="{glow}" stop-opacity=".55"/><stop offset="1" stop-color="{glow}" stop-opacity="0"/></radialGradient>
<radialGradient id="vg{uid}" cx=".5" cy=".5" r=".75"><stop offset=".55" stop-color="#000" stop-opacity="0"/><stop offset="1" stop-color="#000" stop-opacity=".78"/></radialGradient>
<radialGradient id="lg{uid}"><stop offset="0" stop-color="#ffb347" stop-opacity=".75"/><stop offset="1" stop-color="#ffb347" stop-opacity="0"/></radialGradient>
<filter id="bl{uid}" x="-20%" y="-50%" width="140%" height="200%"><feGaussianBlur stdDeviation="14"/></filter>
</defs>"""

def _stars(n, w, h, seed):
    r = random.Random(seed)
    return "".join(f'<circle cx="{r.randint(0,w)}" cy="{r.randint(0,int(h*.55))}" r="{r.choice([.6,.9,1.2])}" fill="#fff" opacity="{r.choice([.25,.4,.6])}"/>' for _ in range(n))

def _moon(uid, cx, cy, r, col="#eadfc8"):
    return f'<circle cx="{cx}" cy="{cy}" r="{r*3.2}" fill="url(#mg{uid})"/><circle cx="{cx}" cy="{cy}" r="{r}" fill="{col}" opacity=".93"/>' \
           f'<circle cx="{cx-r*.3}" cy="{cy-r*.15}" r="{r*.22}" fill="#cbbfa6" opacity=".5"/><circle cx="{cx+r*.28}" cy="{cy+r*.3}" r="{r*.14}" fill="#cbbfa6" opacity=".5"/>'

def _fog(uid, y, col="#cfd3dc", op=.13, w=800):
    return f'<ellipse cx="{w*.28}" cy="{y}" rx="{w*.38}" ry="22" fill="{col}" opacity="{op}" filter="url(#bl{uid})"/><ellipse cx="{w*.78}" cy="{y+14}" rx="{w*.34}" ry="18" fill="{col}" opacity="{op*.9}" filter="url(#bl{uid})"/>'

def _tree(x, y, h, col="#050507", flip=1):
    s = h / 100
    p = [(0,0,0,-100),(0,-55,-38*flip,-95),(0,-70,34*flip,-105),(0,-35,-30*flip,-62),(0,-45,28*flip,-72),(-38*flip,-95,-52*flip,-118),(34*flip,-105,50*flip,-120)]
    d = "".join(f'<line x1="{x+a*s}" y1="{y+b*s}" x2="{x+c*s}" y2="{y+d_*s}" stroke="{col}" stroke-width="{max(1.2,5*s-i*.4)}" stroke-linecap="round"/>'
                for i, (a, b, c, d_) in enumerate([(0,0,0,-60),(0,-30,-30*flip,-70),(0,-45,26*flip,-82),(0,-60,-14*flip,-100),(-30*flip,-70,-48*flip,-86),(26*flip,-82,46*flip,-98),(-14*flip,-100,-30*flip,-118),(0,-60,12*flip,-112)]))
    return d

def _bat(x, y, s=1, col="#050507"):
    return f'<path transform="translate({x},{y}) scale({s})" d="M0 0 C-8 -8 -20 -8 -28 2 C-20 0 -14 3 -10 8 C-6 4 -3 6 0 10 C3 6 6 4 10 8 C14 3 20 0 28 2 C20 -8 8 -8 0 0Z" fill="{col}"/>'

def _house(uid, ox=0, oy=0, k=1.0, lit=True):
    g = f'<g transform="translate({ox},{oy}) scale({k})">'
    g += '<polygon points="285,252 385,176 485,252" fill="#07070a"/><rect x="300" y="250" width="170" height="112" fill="#07070a"/><rect x="430" y="190" width="18" height="40" fill="#07070a"/>'
    g += '<rect x="250" y="216" width="62" height="146" fill="#08080b"/><polygon points="238,220 281,158 324,220" fill="#07070a"/><rect x="276" y="140" width="3" height="22" fill="#07070a"/>'
    g += '<polygon points="470,262 520,262 520,362 470,362" fill="#08080b"/><polygon points="462,264 495,226 528,264" fill="#07070a"/>'
    if lit:
        for (x, y, w, h) in [(330, 282, 26, 36), (415, 282, 26, 36), (266, 246, 18, 28), (486, 292, 18, 26)]:
            g += f'<circle cx="{x+w/2}" cy="{y+h/2}" r="{w*1.7}" fill="url(#lg{uid})"/><rect x="{x}" y="{y}" width="{w}" height="{h}" fill="#f2b04a" opacity=".92"/>'
            g += f'<line x1="{x+w/2}" y1="{y}" x2="{x+w/2}" y2="{y+h}" stroke="#07070a" stroke-width="2"/><line x1="{x}" y1="{y+h/2}" x2="{x+w}" y2="{y+h/2}" stroke="#07070a" stroke-width="2"/>'
    g += '<rect x="372" y="312" width="30" height="50" fill="#030304"/><polygon points="372,312 387,298 402,312" fill="#030304"/>'
    g += "".join(f'<rect x="{x}" y="{338-(i%3)*3}" width="3" height="{26+(i%3)*3}" fill="#050507"/>' for i, x in enumerate(range(90, 252, 14)))
    g += "".join(f'<rect x="{x}" y="{338-(i%3)*3}" width="3" height="{26+(i%3)*3}" fill="#050507"/>' for i, x in enumerate(range(540, 720, 14)))
    g += '<rect x="90" y="346" width="162" height="3" fill="#050507"/><rect x="540" y="346" width="180" height="3" fill="#050507"/></g>'
    return g

def _wrap(uid, body, w=800, h=450):
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" preserveAspectRatio="xMidYMid slice">{body}<rect width="{w}" height="{h}" fill="url(#vg{uid})"/></svg>'

def casita(uid="a"):
    b = _defs(uid, "#14070b", "#3a0f16", "#ffd9b0") + f'<rect width="800" height="450" fill="url(#sky{uid})"/>' + _stars(60, 800, 450, 3) + _moon(uid, 640, 105, 44)
    b += '<path d="M0 372 Q210 322 430 352 T800 336 V450 H0Z" fill="#050507"/>'
    b += _house(uid) + _tree(150, 360, 120, flip=1) + _tree(700, 352, 96, flip=-1)
    b += _bat(520, 80, 1.1) + _bat(560, 130, .8) + _bat(470, 120, .7) + _fog(uid, 372) + _fog(uid, 410, op=.1)
    return _wrap(uid, b)

def hospital(uid="b"):
    b = _defs(uid, "#0a1214", "#1d3a3e", "#bfe9e4") + f'<rect width="800" height="450" fill="url(#sky{uid})"/>' + _stars(40, 800, 450, 5) + _moon(uid, 180, 90, 34, "#d8ece8")
    b += '<path d="M0 380 L800 380 V450 H0Z" fill="#050809"/><rect x="250" y="140" width="300" height="240" fill="#060a0b"/><rect x="200" y="210" width="70" height="170" fill="#070c0d"/><rect x="530" y="200" width="80" height="180" fill="#070c0d"/>'
    r = random.Random(8)
    for row in range(5):
        for col in range(8):
            x, y = 268 + col * 33, 162 + row * 40
            on = r.random() < .22
            b += f'<rect x="{x}" y="{y}" width="20" height="26" fill="{"#b9e8dd" if on else "#0f1a1c"}" opacity="{.85 if on else 1}"/>'
            if on: b += f'<circle cx="{x+10}" cy="{y+13}" r="34" fill="#7fd6c4" opacity=".12"/>'
    b += '<rect x="384" y="96" width="32" height="12" fill="#b3202a"/><rect x="394" y="86" width="12" height="32" fill="#b3202a"/><circle cx="400" cy="102" r="60" fill="#b3202a" opacity=".18" filter="url(#bl%s)"/>' % uid
    b += '<rect x="372" y="330" width="56" height="50" fill="#020303"/>' + "".join(f'<rect x="{x}" y="352" width="3" height="30" fill="#040606"/>' for x in range(60, 200, 14)) + "".join(f'<rect x="{x}" y="352" width="3" height="30" fill="#040606"/>' for x in range(620, 760, 14))
    b += _tree(110, 380, 130) + _tree(700, 380, 110, flip=-1) + _fog(uid, 380, "#a8c9c6") + _fog(uid, 420, "#a8c9c6", .1)
    return _wrap(uid, b)

def _zombie(x, y, s, col="#040405"):
    return (f'<g transform="translate({x},{y}) scale({s})" fill="{col}" stroke="{col}">'
            '<circle cx="0" cy="-92" r="9" stroke="none"/><path d="M-10 -80 L10 -80 L14 -40 L-14 -40Z" stroke="none"/>'
            '<line x1="-8" y1="-76" x2="-40" y2="-70" stroke-width="5" stroke-linecap="round"/><line x1="8" y1="-76" x2="42" y2="-64" stroke-width="5" stroke-linecap="round"/>'
            '<line x1="-6" y1="-40" x2="-14" y2="0" stroke-width="6" stroke-linecap="round"/><line x1="6" y1="-40" x2="12" y2="-2" stroke-width="6" stroke-linecap="round"/></g>')

def zombi(uid="c"):
    b = _defs(uid, "#1a0a05", "#6a1d0e", "#ff8a4c") + f'<rect width="800" height="450" fill="url(#sky{uid})"/>' + _moon(uid, 560, 120, 52, "#f3c9a3")
    r = random.Random(11); x = 0; sk = ""
    while x < 800:
        w = r.randint(40, 90); h = r.randint(90, 230); top = 380 - h
        sk += f'<polygon points="{x},380 {x},{top+r.randint(0,14)} {x+w*.4},{top-r.randint(0,10)} {x+w*.7},{top+r.randint(6,24)} {x+w},{top+r.randint(0,12)} {x+w},380" fill="#070304"/>'
        for _ in range(r.randint(2, 5)):
            sk += f'<rect x="{x+r.randint(6,max(7,w-14))}" y="{top+r.randint(24,max(26,h-40))}" width="7" height="9" fill="#ff9a4c" opacity=".35"/>'
        x += w
    b += sk + '<rect y="378" width="800" height="72" fill="#050304"/>'
    b += "".join(f'<ellipse cx="{cx}" cy="{cy}" rx="46" ry="90" fill="#2a0e07" opacity=".35" filter="url(#bl{uid})"/>' for cx, cy in [(180, 250), (420, 280), (700, 240)])
    b += _zombie(250, 430, 1.25) + _zombie(360, 420, 1.0) + _zombie(480, 430, 1.35) + _zombie(600, 418, .9) + _zombie(120, 425, 1.1) + _fog(uid, 410, "#ff8a4c", .09)
    return _wrap(uid, b)

def experimento(uid="d"):
    b = _defs(uid, "#0b0714", "#2a1647", "#b89cff") + f'<rect width="800" height="450" fill="url(#sky{uid})"/>'
    b += '<polygon points="0,450 800,450 560,320 240,320" fill="#07050d"/>' + "".join(f'<line x1="{x}" y1="450" x2="{400+(x-400)*.3}" y2="320" stroke="#2c1f4a" stroke-width="1"/>' for x in range(-200, 1000, 90))
    b += '<circle cx="400" cy="210" r="150" fill="#0c0818" stroke="#3a2c63" stroke-width="10"/><circle cx="400" cy="210" r="118" fill="none" stroke="#2c2150" stroke-width="3"/><circle cx="400" cy="210" r="70" fill="#09060f" stroke="#4a3a7e" stroke-width="5"/>'
    b += "".join(f'<line x1="400" y1="210" x2="{400+110*__import__("math").cos(a*.7854)}" y2="{210+110*__import__("math").sin(a*.7854)}" stroke="#4a3a7e" stroke-width="5" stroke-linecap="round"/>' for a in range(8))
    b += '<circle cx="400" cy="210" r="16" fill="#6b57b0"/><circle cx="400" cy="210" r="260" fill="url(#mg%s)" opacity=".35"/>' % uid
    b += '<line x1="130" y1="0" x2="130" y2="90" stroke="#1a1230" stroke-width="3"/><circle cx="130" cy="100" r="12" fill="#e9dcff"/><circle cx="130" cy="100" r="70" fill="url(#mg%s)"/>' % uid
    b += "".join(f'<g transform="translate({x},{y})"><rect x="-4" y="-34" width="8" height="22" fill="#05030a"/><path d="M-20 0 Q-22 -14 -4 -16 L4 -16 Q22 -14 20 0 Q18 18 0 18 Q-18 18 -20 0Z" fill="#05030a"/><path d="M-14 2 Q0 12 14 2 Q12 12 0 14 Q-12 12 -14 2Z" fill="#7a5cff" opacity=".55"/></g>' for x, y in [(640, 340), (700, 360), (110, 360)])
    b += _fog(uid, 400, "#b89cff", .1)
    return _wrap(uid, b)

def cuchillo(uid="e"):
    b = _defs(uid, "#0f0604", "#431607", "#ff7a3d") + f'<rect width="800" height="450" fill="url(#sky{uid})"/>' + _stars(30, 800, 450, 9) + _moon(uid, 560, 120, 70, "#f0a57a")
    b += '<g stroke="#7b0d12" stroke-width="5" stroke-linecap="round" opacity=".9"><line x1="520" y1="60" x2="585" y2="190"/><line x1="545" y1="52" x2="610" y2="182"/><line x1="570" y1="46" x2="632" y2="170"/></g>'
    r = random.Random(4)
    def pines(y0, col, n, hmin, hmax):
        s = ""
        for i in range(n):
            x = i * 800 / n + r.randint(-14, 14); h = r.randint(hmin, hmax); w = h * .38
            s += f'<polygon points="{x},{y0} {x-w/2},{y0} {x},{y0-h}" fill="{col}"/><polygon points="{x},{y0} {x+w/2},{y0} {x},{y0-h}" fill="{col}"/>'
            s += f'<polygon points="{x-w*.4},{y0-h*.45} {x+w*.4},{y0-h*.45} {x},{y0-h*.95}" fill="{col}"/>'
        return s
    b += pines(380, "#1a0a07", 20, 90, 150) + '<polygon points="320,332 395,332 395,300 357,272 320,300" fill="#050304"/><rect x="340" y="304" width="16" height="20" fill="#f2a24a"/><circle cx="348" cy="314" r="30" fill="url(#lg%s)"/>' % uid
    b += pines(420, "#060304", 14, 130, 210) + '<rect y="410" width="800" height="40" fill="#040203"/>' + _fog(uid, 400, "#ff9c6b", .08)
    return _wrap(uid, b)

def ritual(uid="f"):
    import math
    b = _defs(uid, "#12040a", "#3b0a16", "#ff5a6e") + f'<rect width="800" height="450" fill="url(#sky{uid})"/>'
    b += '<path d="M300 330 V160 Q400 40 500 160 V330Z" fill="#07030a" stroke="#1d0b14" stroke-width="6"/><path d="M322 330 V170 Q400 70 478 170 V330Z" fill="#0d0509"/>' + _moon(uid, 400, 170, 40, "#f0cfc4")
    b += '<rect y="330" width="800" height="120" fill="#060204"/><ellipse cx="400" cy="372" rx="240" ry="58" fill="none" stroke="#9c1630" stroke-width="3" opacity=".85"/><ellipse cx="400" cy="372" rx="210" ry="48" fill="none" stroke="#9c1630" stroke-width="1.5" opacity=".6"/>'
    pts = [(400 + 214 * math.sin(i * 2 * math.pi * 2 / 5), 372 - 50 * math.cos(i * 2 * math.pi * 2 / 5)) for i in range(5)]
    b += '<polygon points="' + " ".join(f"{x:.0f},{y:.0f}" for x, y in pts) + '" fill="none" stroke="#9c1630" stroke-width="2.5" opacity=".9"/>'
    for i in range(7):
        a = i * 2 * math.pi / 7 + .3; x, y = 400 + 232 * math.sin(a), 372 - 54 * math.cos(a)
        b += f'<circle cx="{x:.0f}" cy="{y-14:.0f}" r="26" fill="url(#lg{uid})"/><rect x="{x-3:.0f}" y="{y-12:.0f}" width="6" height="16" fill="#e9dcc6"/><path d="M{x:.0f} {y-26:.0f} q5 7 0 13 q-5 -6 0 -13z" fill="#ffb347"/>'
    b += _fog(uid, 392, "#ff5a6e", .07)
    return _wrap(uid, b)

def orfanato(uid="g"):
    return casita(uid)

ESCENAS = {1: casita, 2: hospital, 3: zombi, 4: experimento, 5: cuchillo, 6: ritual, 7: orfanato, 8: experimento}

def escena(exp_id):
    return uri(ESCENAS.get(exp_id, casita)(f"x{exp_id}"))

def hero():
    uid = "h"
    b = _defs(uid, "#0b0507", "#34101a", "#ffd9b0") + '<rect width="1600" height="640" fill="url(#skyh)"/>' + _stars(140, 1600, 640, 21) + _moon(uid, 1170, 150, 64)
    b += '<path d="M0 560 Q500 500 900 540 T1600 520 V640 H0Z" fill="#050507"/>'
    b += _house(uid, 700, 90, 1.5) + _tree(640, 560, 190) + _tree(1500, 540, 150, flip=-1) + _tree(1330, 560, 120, flip=1)
    b += _bat(900, 120, 1.6) + _bat(1010, 190, 1.1) + _bat(820, 200, 1.0) + _bat(1320, 300, .9) + _fog(uid, 560, w=1600) + _fog(uid, 610, w=1600, op=.12)
    return uri(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1600 640" preserveAspectRatio="xMidYMid slice">{b}</svg>')

def logo(size=34):
    return (f'<svg width="{size}" height="{size}" viewBox="0 0 40 40" xmlns="http://www.w3.org/2000/svg"><path d="M20 3 L35 16 V36 H5 V16Z" fill="none" stroke="#C1121F" stroke-width="2.2" stroke-linejoin="round"/>'
            '<path d="M20 3 L18 14 L22 20 L19 28 L21 36" fill="none" stroke="#EDE6DA" stroke-width="1.4"/><circle cx="14" cy="24" r="2.2" fill="#C1121F"/><circle cx="26" cy="24" r="2.2" fill="#C1121F"/></svg>')


# ======================================================================================
# 5. PIEZAS VISUALES Y CONTROL DE ACCESO POR ROL
# ======================================================================================
PAGES = {}   # lo llena App.py segun el rol
DIAS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]


def e(x):
    """Escapa texto que viene de la base de datos antes de pintarlo como HTML."""
    if x is None or (isinstance(x, float) and x != x):
        return ""
    return html.escape(str(x))


def cop(v):
    return "$" + f"{float(v):,.0f}".replace(",", ".")


def fecha(d):
    d = d if isinstance(d, (date, datetime)) else None
    return "" if d is None else f"{DIAS[d.weekday()]} {d.day} {MESES[d.month - 1]}"


def fecha_larga(d):
    return "" if d is None else f"{DIAS[d.weekday()]} {d.day} {MESES[d.month - 1]} {d.year}"


def bonito(texto):
    return (texto or "").title()


def rol():
    return st.session_state.get("rol")


def ir(clave):
    st.switch_page(PAGES[clave])


def salir():
    for k in ("rol", "cid", "cnombre", "ultima_venta", "sel_exp", "sel_tipo"):
        st.session_state.pop(k, None)
    st.session_state["_salio"] = True


def exigir(r):
    """Corta la pagina si el rol no corresponde (cliente / admin)."""
    if rol() != r:
        if r == "admin":
            st.error("Esta seccion es solo para el administrador.")
        else:
            st.markdown('<div class="sec"><div class="eyebrow">Acceso</div><h2>Ingresa para continuar</h2></div>', unsafe_allow_html=True)
            st.info("Necesitas ingresar a tu cuenta para ver esta seccion.")
            if "acc" in PAGES:
                st.page_link(PAGES["acc"], label="Ir a ingresar")
        st.stop()


def nav():
    es_admin = rol() == "admin"
    items = (["pan", "aexp", "ses", "ven", "taq", "cli", "aud"] if es_admin else ["inicio", "exp", "res"] + (["mis"] if rol() == "cliente" else []))
    etiquetas = {"inicio": "Inicio", "exp": "Experiencias", "res": "Reservar", "mis": "Mis reservas", "pan": "Panel", "ses": "Sesiones",
                 "aexp": "Cartelera", "ven": "Ventas", "taq": "Taquilla", "cli": "Clientes", "aud": "Auditoría"}
    with st.container(key="nav"):
        anchos = ([3.4] + [1.1] * len(items) + [1.0]) if es_admin else ([2.7] + [1.15] * len(items) + [1.1])
        c = st.columns(anchos, vertical_alignment="center")
        c[0].markdown(f'<div class="brand">{logo()} CASA DEL TERROR' + ('<span class="adm">ADMIN</span>' if es_admin else "") + "</div>", unsafe_allow_html=True)
        for col, k in zip(c[1:-1], items):
            col.page_link(PAGES[k], label=etiquetas[k])
        if rol():
            if c[-1].button("Salir", key="btn_salir", use_container_width=True):
                salir()
                ir("inicio")
        else:
            c[-1].page_link(PAGES["acc"], label="Ingresar")
    mostrar_flash()


def footer():
    st.markdown('<div class="footer"><div><b style="color:#cfc8bb;font-family:Cinzel,serif;letter-spacing:.14em">CASA DEL TERROR</b><br>'
                'Parque temático de experiencias de miedo</div><div class="legal">Proyecto académico — Introducción a BI, EAFIT.<br>'
                'Datos ficticios. Pagos y acceso simulados.</div></div>', unsafe_allow_html=True)


def titulo(eyebrow, h2, sub=None, top=True):
    st.markdown(f'<div class="sec" style="margin-top:{"1.8rem" if top else "2.4rem"}"><div class="eyebrow">{e(eyebrow)}</div><h2>{e(h2)}</h2>'
                + (f'<div class="muted">{e(sub)}</div>' if sub else "") + "</div>", unsafe_allow_html=True)


def pill_estado(estado):
    clase = {"CONFIRMADA": "ok", "ASISTIÓ": "ok", "PROGRAMADA": "ok", "ACTIVA": "ok", "EN CURSO": "warn", "NO ASISTIÓ": "warn",
             "MANTENIMIENTO": "warn", "CANCELADA": "red", "FINALIZADA": "out", "INACTIVA": "out"}.get(estado, "")
    return f'<span class="pill {clase}">{e(bonito(estado))}</span>'


def tarjeta(x, proxima=None):
    """Tarjeta de experiencia. x: dict con columnas ex_*."""
    est = x["ex_estado"]
    badge = {"ACTIVA": '<span class="pill ok">Disponible</span>', "MANTENIMIENTO": '<span class="pill warn">En mantenimiento</span>',
             "INACTIVA": '<span class="pill out">Fuera de cartelera</span>'}[est]
    sig = ""
    if est == "ACTIVA":
        sig = f'<div class="next">{("Próxima sesión: " + e(proxima)) if proxima else "Sin sesiones programadas por ahora"}</div>'
    return (f'<div class="card {"dim" if est != "ACTIVA" else ""}"><div class="img" style="background-image:url({escena(int(x["ex_id_experiencias"]))})"></div>'
            f'<div class="body"><div class="eyebrow">{e(bonito(x["ex_tematica"]))}</div><h3>{e(x["ex_nombre"])}</h3>'
            f'<div class="desc">{e(x["ex_descripcion"])}</div>'
            f'<div class="meta"><span>{int(x["ex_duracion_minutos"])} min</span><span>Hasta {int(x["ex_capacidad_maxima"])} pers.</span><span>+{int(x["ex_edad_minima"])} años</span></div>'
            f'<div style="display:flex;justify-content:space-between;align-items:center"><div class="price">{cop(x["ex_precio_base"])}'
            f'<span class="muted" style="font-size:.7rem;font-family:Inter"> /persona</span></div>{badge}</div>{sig}</div></div>')


def intentar(fn, *args, **kwargs):
    """Ejecuta una operacion de reglas.py. Devuelve (True, resultado) o (False, None) tras mostrar el error."""
    try:
        return True, fn(*args, **kwargs)
    except ReglaError as ex:
        st.error(str(ex))
    except oracledb.Error as ex:
        st.error("No pudimos completar la operacion por un problema con la base de datos. Intenta de nuevo.")
        with st.expander("Detalle tecnico"):
            st.code(str(ex))
    return False, None


# ======================================================================================
# 6. ESTILOS (tema de terror: rojo sangre sobre negro)
# ======================================================================================
CSS = """
<style>
header[data-testid="stHeader"]{display:none}
[data-testid="stToolbar"],[data-testid="stDecoration"],#MainMenu,footer{display:none!important}
.block-container{max-width:1180px;padding:0 1.5rem 3rem!important}
h1,h2,h3{font-family:Cinzel,serif!important;letter-spacing:.04em;font-weight:600!important}
.eyebrow{font-size:.72rem;letter-spacing:.28em;text-transform:uppercase;color:#C1121F;font-weight:600;margin-bottom:.3rem}
.muted{color:#9a948a}
.st-key-nav{border-bottom:1px solid #22222b;padding:.55rem 0;background:#0A0A0D;position:sticky;top:0;z-index:50}
.brand svg{flex-shrink:0}
.brand{display:flex;align-items:center;gap:.6rem;font-family:Cinzel,serif;font-size:1.1rem;letter-spacing:.14em;font-weight:700;color:#EDE6DA;white-space:nowrap}
.st-key-nav a p,.st-key-nav button p{font-size:.74rem!important;letter-spacing:.06em;text-transform:uppercase;white-space:nowrap}
.st-key-nav [data-testid="stPageLink-NavLink"]{justify-content:center;padding:.35rem .4rem;border-radius:3px}
.st-key-nav [data-testid="stPageLink-NavLink"][aria-current="page"]{background:#1c0a0d;border-bottom:2px solid #C1121F;border-radius:3px 3px 0 0}
.st-key-nav button{min-height:2rem;padding:.2rem .7rem;border-color:#3a3a45}
.hero{position:relative;margin:0 -1.5rem 2rem;height:420px;background-size:cover;background-position:center;display:flex;align-items:center;padding:0 3.5rem;border-bottom:1px solid #2a1015}
.hero:before{content:"";position:absolute;inset:0;background:linear-gradient(90deg,rgba(10,10,13,.92) 0%,rgba(10,10,13,.55) 48%,rgba(10,10,13,.1) 100%)}
.hero>div{position:relative;max-width:580px}
.hero h1{font-size:3.2rem;line-height:1.05;margin:.2rem 0 1rem;color:#f3ece0;text-shadow:0 2px 24px #000}
.hero p{color:#d6cfc2;font-size:1.05rem;line-height:1.6}
.pill{display:inline-block;padding:.18rem .6rem;border:1px solid #3a3a45;border-radius:99px;font-size:.72rem;letter-spacing:.08em;color:#cfc8bb;margin:0 .35rem .25rem 0}
.pill.red{border-color:#7b1017;color:#ff6b73;background:#1c0a0d}
.pill.ok{border-color:#1f5a3a;color:#5fd38f;background:#0b1a12}
.pill.warn{border-color:#6a4f13;color:#e8b84a;background:#1a1405}
.pill.out{border-color:#3a3a45;color:#8a8478;background:#121216}
.card{background:#131318;border:1px solid #22222b;border-radius:6px;overflow:hidden;margin-bottom:.6rem}
.card .img{height:165px;background-size:cover;background-position:center;position:relative}
.card .img:after{content:"";position:absolute;inset:0;background:linear-gradient(180deg,transparent 40%,#131318)}
.card .body{padding:1rem 1.15rem 1.1rem}
.card h3{font-size:1.08rem;margin:.1rem 0 .35rem;color:#f3ece0}
.card .desc{color:#9a948a;font-size:.86rem;min-height:5.3em}
.card .meta{display:flex;gap:.9rem;color:#9a948a;font-size:.8rem;margin:.5rem 0}
.card .price{font-family:Cinzel,serif;font-size:1.25rem;color:#EDE6DA}
.card .next{font-size:.78rem;color:#cfc8bb;margin-top:.55rem;border-top:1px dashed #26262e;padding-top:.5rem;min-height:2.2em}
.card.dim{opacity:.55;filter:grayscale(.7)}
.sec{margin:2.4rem 0 1.1rem}
.sec h2{font-size:1.7rem;margin:.1rem 0}
.step{display:flex;gap:.9rem;align-items:flex-start;background:#131318;border:1px solid #22222b;border-radius:6px;padding:1.1rem}
.step b.n{font-family:Cinzel,serif;font-size:1.6rem;color:#C1121F;line-height:1}
.sum{background:#131318;border:1px solid #2a1015;border-radius:6px;padding:1.2rem 1.3rem}
.sum .row{display:flex;justify-content:space-between;gap:1rem;padding:.35rem 0;border-bottom:1px dashed #26262e;font-size:.92rem}
.sum .tot{display:flex;justify-content:space-between;padding-top:.8rem;font-family:Cinzel,serif;font-size:1.5rem}
.st-key-resumen{position:sticky;top:4.2rem}
.st-key-slots [data-testid="stRadioGroup"]{gap:.45rem}
.st-key-slots label[data-testid="stRadioOption"]{width:100%;background:#0f0f13;border:1px solid #22222b;border-radius:5px;padding:.7rem 1rem;margin:0;box-sizing:border-box}
.st-key-slots label[data-testid="stRadioOption"][data-selected="true"]{border-color:#C1121F;background:#1a0a0d}
.st-key-slots label[data-testid="stRadioOption"]>div>div:first-child{display:none}
.ticket{display:flex;background:#131318;border:1px solid #2a2a33;border-radius:8px;overflow:hidden}
.ticket .art{width:300px;background-size:cover;background-position:center}
.ticket .main{flex:1;padding:1.6rem 1.8rem;border-right:2px dashed #2f2f39}
.ticket .stub{width:230px;padding:1.4rem;text-align:center;background:#0f0f13;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:.5rem}
.ticket h2{font-size:1.8rem;margin:.2rem 0 1rem}
.kv{display:grid;grid-template-columns:repeat(3,1fr);gap:1rem}
.kv small{display:block;color:#8a8478;letter-spacing:.14em;text-transform:uppercase;font-size:.66rem}
.kv span{font-size:1.02rem;color:#f3ece0}
.footer{margin:4rem -1.5rem -3rem;padding:2rem 3.5rem;border-top:1px solid #22222b;background:#07070a;color:#8a8478;font-size:.82rem;display:flex;justify-content:space-between;gap:2rem}
.stButton>button[kind="primary"],.stFormSubmitButton>button[kind="primary"]{background:#C1121F;border:1px solid #C1121F;font-weight:600;letter-spacing:.08em;text-transform:uppercase;font-size:.8rem;color:#fff}
.stButton>button[kind="primary"]:hover,.stFormSubmitButton>button[kind="primary"]:hover{background:#e01b2a;border-color:#e01b2a}
[data-testid="stMetric"]{background:#131318;border:1px solid #22222b;border-radius:6px;padding:1rem 1.2rem}
[data-testid="stMetricLabel"]{color:#9a948a;text-transform:uppercase;letter-spacing:.12em;font-size:.7rem}
[data-testid="stMetricValue"]{font-family:Cinzel,serif;font-size:1.65rem}
[data-testid="stMetricLabel"] p{white-space:normal}
.adm{background:#1c0a0d;border:1px solid #7b1017;color:#ff6b73;padding:.15rem .6rem;border-radius:3px;font-size:.66rem;letter-spacing:.18em;font-weight:700;margin-left:.4rem}
.legal{font-size:.78rem;color:#7d786e;line-height:1.5}
.info-box{background:#131318;border:1px solid #22222b;border-left:3px solid #C1121F;border-radius:4px;padding:.9rem 1.1rem;font-size:.9rem;color:#cfc8bb}
@media (max-width:900px){.hero{padding:0 1.5rem}.hero h1{font-size:2.2rem}.ticket{flex-direction:column}.ticket .art{width:100%;height:160px}.ticket .main{border-right:0}.ticket .stub{width:auto}.kv{grid-template-columns:1fr 1fr}.footer{flex-direction:column}}
</style>
"""


def inyectar_estilos():
    st.markdown(CSS, unsafe_allow_html=True)


# ======================================================================================
# 7. PANTALLAS DEL CLIENTE
# ======================================================================================
SQL_EXPERIENCIAS = f"""
SELECT e.ex_id_experiencias, e.ex_nombre, e.ex_tematica, e.ex_descripcion, e.ex_duracion_minutos, e.ex_precio_base,
       e.ex_capacidad_maxima, e.ex_edad_minima, e.ex_estado,
       (SELECT MIN({inicio_sql()}) FROM sesion s
         WHERE s.s_id_experiencias = e.ex_id_experiencias AND s.s_estado = 'PROGRAMADA' AND s.s_cupos_disponibles > 0
           AND {inicio_sql()} > SYSDATE
           AND NOT EXISTS (SELECT 1 FROM venta v JOIN tipo_experiencia t ON t.te_id_tipos_experiencia = v.v_id_tipos_experiencia
                           WHERE v.v_id_sesiones = s.s_id_sesiones AND v.v_estado <> 'CANCELADA' AND t.te_permite_personalizacion = 'SI')) AS proxima,
       (SELECT NVL(SUM(v.v_cantidad_personas), 0) FROM venta v WHERE v.v_id_experiencias = e.ex_id_experiencias AND v.v_estado <> 'CANCELADA') AS vendidas
FROM experiencia e ORDER BY e.ex_id_experiencias"""


def _experiencias():
    df = consultar(SQL_EXPERIENCIAS).astype(object).where(lambda d: d.notna(), None)
    return df.to_dict("records")


def _proxima(r):
    p = r["proxima"]
    return None if p is None or pd.isna(p) else f"{fecha(p)} · {p:%H:%M}"


def _reservar_exp(exp_id, tipo=None):
    st.session_state["sel_exp"] = int(exp_id)
    if tipo:
        st.session_state["sel_tipo"] = tipo
    ir("res")


# ------------------------------------------------------------------ INICIO
def inicio():
    if rol() == "admin":
        ir("pan")
    nav()
    exps = _experiencias()
    activas = [x for x in exps if x["ex_estado"] == "ACTIVA"]
    k = fila(f"""SELECT (SELECT COUNT(*) FROM sesion s WHERE s.s_estado='PROGRAMADA' AND {inicio_sql()} > SYSDATE) AS sesiones,
                        (SELECT NVL(SUM(v_cantidad_personas),0) FROM venta WHERE v_estado IN ('ASISTI'||UNISTR('\\00D3'))) AS visitantes
                 FROM dual""")
    proxima = min((x["proxima"] for x in activas if x["proxima"] is not None), default=None)
    st.markdown(f'''<div class="hero" style="background-image:url({hero()})"><div>
<div class="eyebrow">Parque temático de experiencias de miedo</div><h1>Entra. Si te atreves a salir.</h1>
<p>{len(activas)} recorridos inmersivos con salas ambientadas, actores en vivo y efectos especiales. Compra tu entrada o reserva un recorrido privado, a tu medida, para tu grupo.</p>
<div style="margin-top:1.3rem">{('<span class="pill red">Próxima sesión: ' + e(fecha(proxima)) + " · " + f"{proxima:%H:%M}" + "</span>") if proxima is not None else ""}</div></div></div>''', unsafe_allow_html=True)
    b = st.columns([1.3, 1.3, 4])
    if b[0].button("Comprar entradas", type="primary", key="h1"):
        ir("res")
    if b[1].button("Reserva privada", key="h2"):
        st.session_state["sel_tipo"] = "RESERVA"
        ir("res")
    titulo("Cartelera", "Los más buscados", top=False)
    top = sorted(activas, key=lambda x: -x["vendidas"])[:3]
    cols = st.columns(3, gap="large")
    for c, x in zip(cols, top):
        c.markdown(tarjeta(x, _proxima(x)), unsafe_allow_html=True)
        if c.button("Reservar", key=f"i{x['ex_id_experiencias']}", width="stretch"):
            _reservar_exp(x["ex_id_experiencias"])
    titulo("Así funciona", "Tres pasos para vivir el miedo", top=False)
    cols = st.columns(3, gap="large")
    for c, (n, t, d) in zip(cols, [("1", "Elige tu experiencia", "Revisa duración, edad mínima y precio de cada recorrido."),
                                    ("2", "Escoge fecha y hora", "Ves en tiempo real los cupos que quedan en cada sesión."),
                                    ("3", "Recibe tu entrada", "Pagas en línea y obtienes un código para presentar en la puerta.")]):
        c.markdown(f'<div class="step"><b class="n">{n}</b><div><b style="font-size:1rem;color:#f3ece0">{t}</b><div class="muted" style="font-size:.88rem;margin-top:.3rem">{d}</div></div></div>', unsafe_allow_html=True)
    titulo("En números", "La casa hoy", top=False)
    m = st.columns(4)
    m[0].metric("Experiencias en cartelera", len(activas))
    m[1].metric("Sesiones por venir", int(k["sesiones"]))
    m[2].metric("Visitantes que ya entraron", int(k["visitantes"]))
    m[3].metric("Desde", cop(min(x["ex_precio_base"] for x in activas)) if activas else "-")
    footer()


# ------------------------------------------------------------------ EXPERIENCIAS
def experiencias():
    nav()
    titulo("Cartelera", "Experiencias", "Elige un recorrido y mira sus próximas sesiones.")
    exps = _experiencias()
    tem = sorted({x["ex_tematica"] for x in exps if x["ex_estado"] == "ACTIVA"})
    sel = st.pills("Temática", ["Todas"] + tem, default="Todas", format_func=lambda t: bonito(t), key="f_tem")
    f = st.columns([1.5, 1.3, 3], vertical_alignment="bottom")
    orden = f[0].selectbox("Ordenar por", ["Precio: menor a mayor", "Precio: mayor a menor", "Duración", "Edad mínima"])
    solo = f[1].toggle("Solo disponibles", value=True)
    key = {"Precio: menor a mayor": lambda x: x["ex_precio_base"], "Precio: mayor a menor": lambda x: -x["ex_precio_base"],
           "Duración": lambda x: x["ex_duracion_minutos"], "Edad mínima": lambda x: x["ex_edad_minima"]}[orden]
    lista = [x for x in exps if (not solo or x["ex_estado"] == "ACTIVA") and (sel in (None, "Todas") or x["ex_tematica"] == sel)]
    lista.sort(key=lambda x: (x["ex_estado"] != "ACTIVA", key(x)))
    if not lista:
        st.info("No hay experiencias con ese filtro.")
    for i in range(0, len(lista), 3):
        cols = st.columns(3, gap="large")
        for c, x in zip(cols, lista[i:i + 3]):
            c.markdown(tarjeta(x, _proxima(x)), unsafe_allow_html=True)
            if x["ex_estado"] == "ACTIVA":
                if c.button("Ver sesiones y reservar", key=f"x{x['ex_id_experiencias']}", width="stretch"):
                    _reservar_exp(x["ex_id_experiencias"])
            else:
                c.button("No disponible", key=f"x{x['ex_id_experiencias']}", disabled=True, width="stretch")
    footer()


# ------------------------------------------------------------------ RESERVAR
def _edad_en(nac, d):
    return None if nac is None or pd.isna(nac) else d.year - nac.year - ((d.month, d.day) < (nac.month, nac.day))


def reservar():
    nav()
    titulo("Reservas", "Reserva tu experiencia")
    if rol() != "cliente":
        exigir("cliente")
    cid = st.session_state["cid"]
    exps = consultar("""SELECT ex_id_experiencias AS id, ex_nombre AS nombre, ex_tematica AS tematica, ex_descripcion AS descripcion,
                        ex_duracion_minutos AS dur, ex_precio_base AS precio, ex_capacidad_maxima AS cap, ex_edad_minima AS edad
                        FROM experiencia WHERE ex_estado = 'ACTIVA' ORDER BY ex_nombre""")
    tipos = consultar("""SELECT te_id_tipos_experiencia AS id, te_nombre AS nombre, te_descripcion AS descripcion, te_permite_personalizacion AS pers,
                         te_requiere_reserva AS reserva, te_recargo_porcentaje AS pct FROM tipo_experiencia ORDER BY te_id_tipos_experiencia""")
    if exps.empty:
        st.info("Por ahora no hay experiencias disponibles. Vuelve pronto.")
        st.stop()
    ids = [int(i) for i in exps["id"]]
    ex = {int(r["id"]): r for r in exps.to_dict("records")}
    tp = {int(r["id"]): r for r in tipos.to_dict("records")}
    t_general = next(i for i, r in tp.items() if r["reserva"] == "NO")
    t_reserva = next(i for i, r in tp.items() if r["reserva"] == "SI")
    if st.session_state.get("sel_exp") in ids:
        st.session_state["r_exp"] = st.session_state.pop("sel_exp")
    if st.session_state.get("sel_tipo") == "RESERVA":
        st.session_state["r_tipo"] = t_reserva
        st.session_state.pop("sel_tipo")
    st.session_state.setdefault("r_tipo", t_general)

    L, R = st.columns([1.6, 1], gap="large")
    with L:
        eid = st.selectbox("Experiencia", ids, format_func=lambda i: ex[i]["nombre"], key="r_exp")
        tid = st.segmented_control("Tipo de entrada", [t_general, t_reserva], format_func=lambda i: bonito(tp[i]["nombre"]), key="r_tipo")
        tid = tid or t_general
        t, x = tp[tid], ex[eid]
        st.markdown(f'<div class="card" style="display:flex;margin:.4rem 0 1rem"><div style="width:200px;background:url({escena(eid)}) center/cover"></div>'
                    f'<div class="body"><div class="eyebrow">{e(bonito(x["tematica"]))}</div><h3>{e(x["nombre"])}</h3><div class="desc" style="min-height:0">{e(x["descripcion"])}</div>'
                    f'<div class="meta"><span>{int(x["dur"])} min</span><span>Hasta {int(x["cap"])} personas</span><span>+{int(x["edad"])} años</span></div></div></div>', unsafe_allow_html=True)
        st.markdown(f'<div class="info-box"><b>{e(bonito(t["nombre"]))}.</b> {e(t["descripcion"])}'
                    + (f' Recargo de la reserva: <b>{float(t["pct"]):g} %</b>.' if float(t["pct"]) > 0 else "") + "</div>", unsafe_allow_html=True)
        st.write("")
        ses = sesiones_disponibles(eid, t["reserva"] == "SI")
        st.markdown("**Fecha y hora**")
        sid, s = None, None
        if ses.empty:
            st.info("No hay sesiones disponibles para esta combinación. " + (
                "Para una reserva personalizada se necesita una sesión sin entradas vendidas y con al menos "
                f"{HORAS_ANTICIPACION_RESERVA} horas de anticipación; prueba con otra experiencia o con entrada general." if t["reserva"] == "SI"
                else "Prueba con otra experiencia."))
        else:
            filas = {int(r["id"]): r for r in ses.to_dict("records")}

            def etiqueta(i):
                r = filas[i]
                cupo = "sesión privada para tu grupo" if t["reserva"] == "SI" else (f"últimos {int(r['cupos'])} cupos" if r["cupos"] <= 3 else f"{int(r['cupos'])} cupos")
                return f"{fecha(r['fecha'])} · {r['hora']}   |   {bonito(r['zona'])}   |   {cupo}"
            with st.container(key="slots"):
                sid = st.radio("Sesiones", list(filas), format_func=etiqueta, label_visibility="collapsed", key=f"r_ses_{eid}_{tid}")
            s = filas[sid]
        st.write("")
        maxp = min(MAX_PERSONAS_COMPRA, int(x["cap"]), int(s["cupos"]) if s is not None else 1)
        a = st.columns(2)
        n = a[0].number_input("Número de personas", 1, max(1, maxp), min(2, max(1, maxp)), key=f"r_n_{eid}_{tid}_{sid}",
                              help=f"Máximo {MAX_PERSONAS_COMPRA} por compra y según los cupos de la sesión.")
        pago = a[1].selectbox("Método de pago", METODOS_WEB, format_func=bonito, key="r_pago")
        inten, actores, efectos, motivo, notas = None, False, False, None, None
        if t["pers"] == "SI":
            st.markdown("**Personaliza tu recorrido**")
            inten = st.segmented_control("Intensidad", INTENSIDADES, format_func=bonito, default="MEDIA", key="r_int") or "MEDIA"
            q = st.columns(3)
            actores = q[0].toggle("Actores en vivo", key="r_act")
            efectos = q[1].toggle("Efectos especiales", key="r_efe")
            motivo = q[2].selectbox("Motivo", MOTIVOS, format_func=bonito, key="r_mot")
            notas = st.text_area("Notas para el equipo (opcional)", max_chars=500, key="r_not", placeholder="Ej.: vamos disfrazados, llegaremos 10 minutos antes...")
    with R:
        with st.container(key="resumen"):
            recargo, total = calcular_precio(t["pct"], n, x["precio"])
            filas_res = [("Sesión", f"{fecha(s['fecha'])} · {s['hora']}" if s is not None else "—"), ("Personas", str(int(n))),
                         ("Valor unitario", cop(x["precio"])), (f"Recargo ({float(t['pct']):g} %)" if float(t["pct"]) else "Recargo", cop(recargo))]
            st.markdown('<div class="sum"><div class="eyebrow">Resumen</div>' + f'<h3 style="margin:.2rem 0 .8rem">{e(x["nombre"])}</h3>'
                        + "".join(f'<div class="row"><span class="muted">{e(a_)}</span><span>{e(b_)}</span></div>' for a_, b_ in filas_res)
                        + f'<div class="tot"><span>Total</span><span>{cop(total)}</span></div></div>', unsafe_allow_html=True)
            st.write("")
            bloqueo = s is None
            if s is not None:
                nac = fila("SELECT c_fecha_nacimiento AS n FROM cliente WHERE c_id_clientes=:i", i=cid)["n"]
                edad = _edad_en(nac, s["fecha"])
                if edad is None or edad < int(x["edad"]):
                    st.warning(f"Esta experiencia es para mayores de {int(x['edad'])} años (edad a la fecha de la sesión).")
                    bloqueo = True
            if st.button("Pagar y confirmar", type="primary", width="stretch", disabled=bloqueo, key="r_pagar"):
                ok, r = intentar(comprar, cid, int(sid), int(tid), int(n), pago, intensidad=inten, actores=actores, efectos=efectos, motivo=motivo, notas=notas)
                if ok:
                    st.session_state["ultima_venta"] = r["id"]
                    flash("¡Pago aprobado! Tu reserva quedó confirmada.")
                    ir("ent")
            st.markdown('<div class="legal" style="margin-top:.8rem">'
                        + ("Reserva personalizada: la sesión es privada para tu grupo. Puedes cancelarla hasta "
                           f"{HORAS_CANCELACION_CLIENTE} horas antes." if t["reserva"] == "SI" else "Entrada general: no admite cambios ni cancelaciones.")
                        + " Pago simulado con fines académicos.</div>", unsafe_allow_html=True)
    footer()


# ------------------------------------------------------------------ ENTRADA (comprobante)
def _qr(texto):
    buf = io.BytesIO()
    segno.make(texto, error="m").save(buf, kind="svg", scale=5, dark="#EDE6DA", light="#0f0f13", border=1)
    return "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode()


def entrada():
    nav()
    exigir("cliente")
    vid = st.session_state.get("ultima_venta")
    v = None
    if vid:   # N17: solo se muestra si la venta es del cliente que ingreso
        v = fila(f"""SELECT v.v_id_ventas AS id, v.v_cantidad_personas AS n, v.v_valor_unitario AS unit, v.v_recargo AS recargo, v.v_valor_total AS total,
                            v.v_nivel_intensidad AS inten, v.v_actores_en_vivo AS actores, v.v_efectos_especiales AS efectos, v.v_motivo_celebracion AS motivo,
                            v.v_notas AS notas, v.v_metodo_pago AS pago, v.v_estado AS estado, e.ex_id_experiencias AS eid, e.ex_nombre AS exp,
                            s.s_fecha AS fecha, s.s_hora_inicio AS hora, s.s_zona AS zona, t.te_nombre AS tipo, t.te_recargo_porcentaje AS pct
                     FROM venta v JOIN sesion s ON s.s_id_sesiones = v.v_id_sesiones JOIN experiencia e ON e.ex_id_experiencias = v.v_id_experiencias
                     JOIN tipo_experiencia t ON t.te_id_tipos_experiencia = v.v_id_tipos_experiencia
                     WHERE v.v_id_ventas = :v AND v.v_id_clientes = :c""", v=int(vid), c=st.session_state["cid"])
    if not v:
        titulo("Entrada", "Aún no hay una entrada para mostrar")
        st.info("Elige una compra desde «Mis reservas» o haz una nueva reserva.")
        st.page_link(PAGES["res"], label="Reservar ahora")
        footer()
        return
    codigo = f"CT-{int(v['id']):06d}"
    activa = v["estado"] != "CANCELADA"
    titulo("Comprobante", "¡Tu reserva está confirmada!" if v["estado"] == "CONFIRMADA" else "Tu entrada",
              "Presenta este código en la puerta. También lo encuentras en «Mis reservas»." if activa else "Esta reserva fue cancelada.")
    st.markdown(pill_estado(v["estado"]), unsafe_allow_html=True)
    pers = ""
    if v["inten"]:
        pers = (f'<div style="margin-top:1rem" class="muted">Personalización: intensidad {e(bonito(v["inten"]))}'
                f'{", actores en vivo" if v["actores"] == "SI" else ""}{", efectos especiales" if v["efectos"] == "SI" else ""}'
                f'{(" · " + e(bonito(v["motivo"]))) if v["motivo"] and v["motivo"] != "NINGUNO" else ""}</div>')
    st.markdown(f'''<div class="ticket"><div class="art" style="background-image:url({escena(int(v['eid']))})"></div>
<div class="main"><div class="eyebrow">{e(bonito(v['tipo']))}</div><h2>{e(v['exp'])}</h2>
<div class="kv"><div><small>Fecha</small><span>{e(fecha_larga(v['fecha']))}</span></div><div><small>Hora</small><span>{e(v['hora'])}</span></div><div><small>Sala</small><span>{e(bonito(v['zona']))}</span></div>
<div><small>Personas</small><span>{int(v['n'])}</span></div><div><small>Pago</small><span>{e(bonito(v['pago']))}</span></div><div><small>Código</small><span>{codigo}</span></div></div>{pers}
<div class="sum" style="margin-top:1.3rem;padding:.7rem 1rem"><div class="row"><span class="muted">Subtotal ({int(v['n'])} × {cop(v['unit'])})</span><span>{cop(float(v['n']) * float(v['unit']))}</span></div>
<div class="row"><span class="muted">Recargo{(" (" + format(float(v["pct"]), "g") + " %)") if float(v["recargo"]) else ""}</span><span>{cop(v['recargo'])}</span></div>
<div class="tot" style="font-size:1.2rem"><span>Total</span><span>{cop(v['total'])}</span></div></div></div>
<div class="stub"><img src="{_qr(codigo)}" width="150" alt="Código QR"><div style="font-family:Cinzel,serif;letter-spacing:.2em;font-size:1.05rem">{codigo}</div><div class="legal">Válido solo para la sesión indicada</div></div></div>''', unsafe_allow_html=True)
    st.write("")
    b = st.columns([1.3, 1.3, 4])
    if b[0].button("Ver mis reservas", type="primary", key="e_mis"):
        ir("mis")
    if b[1].button("Seguir comprando", key="e_res"):
        ir("res")
    footer()


# ------------------------------------------------------------------ MIS RESERVAS
@st.dialog("Cancelar reserva")
def _dlg_cancelar(vid, nombre, cupos):
    st.write(f"¿Seguro que quieres cancelar tu reserva de **{nombre}**? Se liberarán los {cupos} cupos y esta acción no se puede deshacer.")
    c = st.columns(2)
    if c[0].button("Sí, cancelar reserva", type="primary", width="stretch"):
        ok, _ = intentar(cancelar_venta, int(vid), cliente_id=st.session_state["cid"])
        if ok:
            flash("Tu reserva fue cancelada y los cupos quedaron liberados.")
            st.rerun()
    if c[1].button("No, conservarla", width="stretch"):
        st.rerun()


def mis():
    nav()
    exigir("cliente")
    cid = st.session_state["cid"]
    yo = fila("SELECT c_tipo_documento AS td, c_numero_documento AS doc, c_nombre AS n, c_apellido AS a, c_correo AS correo, c_telefono AS tel FROM cliente WHERE c_id_clientes=:i", i=cid)
    titulo("Mi cuenta", f"Hola, {yo['n']}", f"{yo['correo'] or ''} · {yo['td']} {yo['doc']}")
    df = consultar(f"""
        SELECT v.v_id_ventas AS id, v.v_cantidad_personas AS n, v.v_valor_total AS total, v.v_estado AS estado, v.v_metodo_pago AS pago,
               v.v_fecha_registro AS registro, e.ex_id_experiencias AS eid, e.ex_nombre AS exp, s.s_fecha AS fecha, s.s_hora_inicio AS hora,
               s.s_zona AS zona, s.s_estado AS sest, t.te_nombre AS tipo, t.te_requiere_reserva AS reserva, ({inicio_sql()} - SYSDATE) * 24 AS horas
        FROM venta v JOIN sesion s ON s.s_id_sesiones = v.v_id_sesiones JOIN experiencia e ON e.ex_id_experiencias = v.v_id_experiencias
        JOIN tipo_experiencia t ON t.te_id_tipos_experiencia = v.v_id_tipos_experiencia
        WHERE v.v_id_clientes = :c ORDER BY s.s_fecha DESC, s.s_hora_inicio DESC""", c=cid)
    t1, t2, t3 = st.tabs(["Próximas", "Historial", "Mis datos"])
    with t1:
        prox = df[(df["estado"] == "CONFIRMADA") & (df["sest"] == "PROGRAMADA") & (df["horas"] > 0)].sort_values("horas") if not df.empty else df
        if prox.empty:
            st.info("No tienes reservas próximas.")
            st.page_link(PAGES["res"], label="Reservar una experiencia")
        for v in prox.to_dict("records"):
            es_reserva = v["reserva"] == "SI"
            puede = es_reserva and v["horas"] >= HORAS_CANCELACION_CLIENTE
            with st.container(border=True):
                a = st.columns([1.1, 3, 1.3, 1.6], vertical_alignment="center")
                a[0].markdown(f'<div style="height:96px;border-radius:5px;background:url({escena(int(v["eid"]))}) center/cover"></div>', unsafe_allow_html=True)
                a[1].markdown(f'<div class="eyebrow">{e(bonito(v["tipo"]))}</div><b style="font-size:1.1rem;color:#f3ece0">{e(v["exp"])}</b>'
                              f'<div class="muted" style="font-size:.85rem">{e(fecha(v["fecha"]))} · {e(v["hora"])} · {int(v["n"])} {"persona" if v["n"] == 1 else "personas"}</div>'
                              f'<div style="margin-top:.4rem">{pill_estado(v["estado"])}<span class="pill">CT-{int(v["id"]):06d}</span></div>', unsafe_allow_html=True)
                a[2].markdown(f'<div class="muted" style="font-size:.7rem;letter-spacing:.12em">TOTAL</div><div class="price" style="font-family:Cinzel;font-size:1.3rem">{cop(v["total"])}</div>', unsafe_allow_html=True)
                if a[3].button("Ver entrada", key=f"ve{v['id']}", width="stretch"):
                    st.session_state["ultima_venta"] = int(v["id"])
                    ir("ent")
                if es_reserva:
                    if a[3].button("Cancelar reserva", key=f"cx{v['id']}", disabled=not puede, width="stretch"):
                        _dlg_cancelar(int(v["id"]), v["exp"], int(v["n"]))
                    a[3].caption(f"Cancelable hasta {HORAS_CANCELACION_CLIENTE} h antes" if puede else f"Ya no se puede cancelar (menos de {HORAS_CANCELACION_CLIENTE} h)")
                else:
                    a[3].caption("Entrada general: no admite cambios")
    with t2:
        if df.empty:
            st.info("Aún no tienes compras.")
        else:
            h = pd.DataFrame({"Código": [f"CT-{int(i):06d}" for i in df["id"]], "Experiencia": df["exp"], "Tipo": df["tipo"].map(bonito),
                              "Fecha": [fecha_larga(f) for f in df["fecha"]], "Hora": df["hora"], "Personas": df["n"].astype(int),
                              "Total": df["total"].map(cop), "Estado": df["estado"].map(bonito)})
            st.dataframe(h, hide_index=True, width="stretch")
    with t3:
        st.markdown(f"**{e(yo['n'])} {e(yo['a'])}** · {e(yo['td'])} {e(yo['doc'])}", unsafe_allow_html=True)
        st.caption("El documento y la fecha de nacimiento no se pueden cambiar desde aquí.")
        with st.form("datos"):
            correo = st.text_input("Correo electrónico", value=yo["correo"] or "")
            tel = st.text_input("Teléfono", value=yo["tel"] or "")
            if st.form_submit_button("Guardar cambios", type="primary"):
                ok, _ = intentar(actualizar_datos_cliente, cid, correo, tel)
                if ok:
                    flash("Datos actualizados.")
                    st.rerun()
    footer()


# ------------------------------------------------------------------ ACCESO
def _clave_admin():
    if os.environ.get("ADMIN_PASSWORD"):
        return os.environ["ADMIN_PASSWORD"]
    try:
        return st.secrets["admin"]["password"]
    except Exception:
        return None


def acceso():
    nav()
    st.markdown('<div style="margin-top:1.6rem"></div>', unsafe_allow_html=True)
    if rol():
        quien = "administrador" if rol() == "admin" else st.session_state.get("cnombre", "")
        st.success(f"Ingresaste como {quien}.")
        return
    L, R = st.columns([1.05, 1], gap="large")
    with L:
        st.markdown(f'<div style="height:470px;border-radius:6px;border:1px solid #22222b;background:url({escena(6)}) center/cover"></div>', unsafe_allow_html=True)
    with R:
        st.markdown('<div class="eyebrow">Mi cuenta</div><h2>Ingresa o regístrate</h2>', unsafe_allow_html=True)
        t1, t2, t3 = st.tabs(["Ingresar", "Crear cuenta", "Administrador"])
        with t1:
            with st.form("login"):
                doc = st.text_input("Número de documento")
                correo = st.text_input("Correo electrónico")
                if st.form_submit_button("Ingresar", type="primary", width="stretch"):
                    ok, c = intentar(autenticar_cliente, doc, correo)
                    if ok:
                        st.session_state.update(rol="cliente", cid=int(c["id"]), cnombre=c["nombre"])
                        ir("inicio")
            st.caption("Acceso simulado para el proyecto: se verifican documento y correo contra la base de datos (no es seguridad real).")
        with t2:
            with st.form("registro"):
                a = st.columns([1, 2])
                td = a[0].selectbox("Tipo", TIPOS_DOC)
                dc = a[1].text_input("Número de documento", key="g_doc")
                b = st.columns(2)
                nom, ape = b[0].text_input("Nombre", key="g_nom"), b[1].text_input("Apellido", key="g_ape")
                cr, tel = st.text_input("Correo electrónico", key="g_cor"), st.text_input("Teléfono (opcional)", key="g_tel")
                nac = st.date_input("Fecha de nacimiento", value=None, min_value=date(1920, 1, 1), max_value=date.today(), format="DD/MM/YYYY", key="g_nac",
                                    help="Se usa para validar la edad mínima de cada experiencia.")
                if st.form_submit_button("Crear cuenta", type="primary", width="stretch"):
                    ok, nuevo = intentar(registrar_cliente, td, dc, nom, ape, cr, tel, nac)
                    if ok:
                        st.session_state.update(rol="cliente", cid=int(nuevo), cnombre=(nom or "").strip())
                        flash("¡Cuenta creada! Ya puedes reservar.")
                        ir("inicio")
        with t3:
            with st.form("admin"):
                clave = st.text_input("Clave de administrador", type="password")
                if st.form_submit_button("Entrar al panel", type="primary", width="stretch"):
                    real = _clave_admin()
                    if not real:
                        st.error("No hay clave de administrador configurada en .streamlit/secrets.toml.")
                    elif hmac.compare_digest(clave.encode(), real.encode()):
                        st.session_state.update(rol="admin")
                        ir("pan")
                    else:
                        st.error("Clave incorrecta.")
            st.caption("La clave del administrador se guarda en secrets.toml, no en el código.")
    footer()


# Las 19 reglas R01-R19 que verifica la pantalla 'Auditoria' (misma consulta del script SQL 03)
AUDITORIA_SQL = r"""SELECT regla, descripcion, violaciones
FROM (
    -- R01 ---------------------------------------------------------
    SELECT 'R01' AS regla,
           'Personas por venta <= capacidad maxima de la experiencia' AS descripcion,
           COUNT(*) AS violaciones
    FROM venta v JOIN experiencia e ON e.ex_id_experiencias = v.v_id_experiencias
    WHERE v.v_cantidad_personas > e.ex_capacidad_maxima
    UNION ALL
    -- R02 ---------------------------------------------------------
    SELECT 'R02', 'Personas por venta <= 15 (maximo por compra)', COUNT(*)
    FROM venta WHERE v_cantidad_personas > 15
    UNION ALL
    -- R03 ---------------------------------------------------------
    SELECT 'R03', 'Cupos disponibles = totales - personas de ventas activas', COUNT(*)
    FROM sesion s
    WHERE s.s_cupos_disponibles <> s.s_cupos_totales - NVL((
            SELECT SUM(v.v_cantidad_personas) FROM venta v
            WHERE v.v_id_sesiones = s.s_id_sesiones AND v.v_estado <> 'CANCELADA'), 0)
    UNION ALL
    -- R04 ---------------------------------------------------------
    SELECT 'R04', 'Solo se personaliza si el tipo lo permite', COUNT(*)
    FROM venta v JOIN tipo_experiencia t ON t.te_id_tipos_experiencia = v.v_id_tipos_experiencia
    WHERE t.te_permite_personalizacion = 'NO'
      AND (v.v_nivel_intensidad IS NOT NULL OR v.v_actores_en_vivo = 'SI' OR v.v_efectos_especiales = 'SI'
           OR v.v_notas IS NOT NULL
           OR (v.v_motivo_celebracion IS NOT NULL AND v.v_motivo_celebracion <> 'NINGUNO'))
    UNION ALL
    -- R05 ---------------------------------------------------------
    SELECT 'R05', 'Edad del cliente en la fecha de la sesion >= edad minima (excepto visitante general)', COUNT(*)
    FROM venta v
         JOIN cliente c     ON c.c_id_clientes = v.v_id_clientes
         JOIN experiencia e ON e.ex_id_experiencias = v.v_id_experiencias
         JOIN sesion s      ON s.s_id_sesiones = v.v_id_sesiones
    WHERE c.c_numero_documento <> '0'
      AND (c.c_fecha_nacimiento IS NULL
           OR MONTHS_BETWEEN(s.s_fecha, c.c_fecha_nacimiento) / 12 < e.ex_edad_minima)
    UNION ALL
    -- R06 ---------------------------------------------------------
    SELECT 'R06', 'Recargo = % del tipo x personas x valor unitario', COUNT(*)
    FROM venta v JOIN tipo_experiencia t ON t.te_id_tipos_experiencia = v.v_id_tipos_experiencia
    WHERE v.v_recargo <> ROUND(t.te_recargo_porcentaje / 100 * v.v_cantidad_personas * v.v_valor_unitario, 2)
    UNION ALL
    -- R07 ---------------------------------------------------------
    SELECT 'R07', 'Una sesion CANCELADA no tiene ventas activas', COUNT(*)
    FROM venta v JOIN sesion s ON s.s_id_sesiones = v.v_id_sesiones
    WHERE s.s_estado = 'CANCELADA' AND v.v_estado <> 'CANCELADA'
    UNION ALL
    -- R08 ---------------------------------------------------------
    SELECT 'R08', 'Una sesion vende una sola experiencia (ventas activas)', COUNT(*)
    FROM (SELECT v_id_sesiones FROM venta WHERE v_estado <> 'CANCELADA'
          GROUP BY v_id_sesiones HAVING COUNT(DISTINCT v_id_experiencias) > 1)
    UNION ALL
    -- R09 ---------------------------------------------------------
    SELECT 'R09', 'Una sesion con reserva personalizada activa es privada (una sola venta activa)', COUNT(*)
    FROM (SELECT v.v_id_sesiones
          FROM venta v JOIN tipo_experiencia t ON t.te_id_tipos_experiencia = v.v_id_tipos_experiencia
          WHERE v.v_estado <> 'CANCELADA'
          GROUP BY v.v_id_sesiones
          HAVING SUM(CASE WHEN t.te_permite_personalizacion = 'SI' THEN 1 ELSE 0 END) > 0
             AND COUNT(*) > 1)
    UNION ALL
    -- R10 ---------------------------------------------------------
    SELECT 'R10', 'Reserva personalizada solo para clientes registrados (no visitante general)', COUNT(*)
    FROM venta v
         JOIN tipo_experiencia t ON t.te_id_tipos_experiencia = v.v_id_tipos_experiencia
         JOIN cliente c ON c.c_id_clientes = v.v_id_clientes
    WHERE t.te_requiere_reserva = 'SI' AND c.c_numero_documento = '0'
    UNION ALL
    -- R11 ---------------------------------------------------------
    SELECT 'R11', 'Reserva previa: registrada >= 24 horas antes del inicio de la sesion', COUNT(*)
    FROM venta v
         JOIN tipo_experiencia t ON t.te_id_tipos_experiencia = v.v_id_tipos_experiencia
         JOIN sesion s ON s.s_id_sesiones = v.v_id_sesiones
    WHERE t.te_requiere_reserva = 'SI'
      AND CAST(v.v_fecha_registro AS DATE) >
          TO_DATE(TO_CHAR(s.s_fecha, 'YYYY-MM-DD') || ' ' || s.s_hora_inicio, 'YYYY-MM-DD HH24:MI') - 1
    UNION ALL
    -- R12 ---------------------------------------------------------
    SELECT 'R12', 'Ninguna venta se registra despues del inicio de la sesion', COUNT(*)
    FROM venta v JOIN sesion s ON s.s_id_sesiones = v.v_id_sesiones
    WHERE CAST(v.v_fecha_registro AS DATE) >
          TO_DATE(TO_CHAR(s.s_fecha, 'YYYY-MM-DD') || ' ' || s.s_hora_inicio, 'YYYY-MM-DD HH24:MI')
    UNION ALL
    -- R13 ---------------------------------------------------------
    SELECT 'R13', 'Sesion FINALIZADA sin ventas PENDIENTE/CONFIRMADA (asistencia resuelta)', COUNT(*)
    FROM venta v JOIN sesion s ON s.s_id_sesiones = v.v_id_sesiones
    WHERE s.s_estado = 'FINALIZADA' AND v.v_estado IN ('PENDIENTE', 'CONFIRMADA')
    UNION ALL
    -- R14 ---------------------------------------------------------
    SELECT 'R14', 'Asistencia solo se marca en sesiones EN CURSO o FINALIZADA', COUNT(*)
    FROM venta v JOIN sesion s ON s.s_id_sesiones = v.v_id_sesiones
    WHERE v.v_estado IN ('ASISTI' || UNISTR('\00D3'), 'NO ASISTI' || UNISTR('\00D3'))
      AND s.s_estado NOT IN ('EN CURSO', 'FINALIZADA')
    UNION ALL
    -- R15 ---------------------------------------------------------
    SELECT 'R15', 'El VISITANTE GENERAL existe, esta ACTIVO y tiene documento NA / 0',
           CASE WHEN EXISTS (SELECT 1 FROM cliente
                             WHERE c_numero_documento = '0' AND c_tipo_documento = 'NA'
                               AND c_estado = 'ACTIVO') THEN 0 ELSE 1 END
    FROM dual
    UNION ALL
    -- R16 ---------------------------------------------------------
    SELECT 'R16', 'Clientes INACTIVOS sin ventas activas en sesiones PROGRAMADAS', COUNT(*)
    FROM venta v
         JOIN cliente c ON c.c_id_clientes = v.v_id_clientes
         JOIN sesion s  ON s.s_id_sesiones = v.v_id_sesiones
    WHERE c.c_estado = 'INACTIVO' AND v.v_estado <> 'CANCELADA' AND s.s_estado = 'PROGRAMADA'
    UNION ALL
    -- R17 ---------------------------------------------------------
    SELECT 'R17', 'La experiencia de cada venta es la experiencia de su sesion', COUNT(*)
    FROM venta v JOIN sesion s ON s.s_id_sesiones = v.v_id_sesiones
    WHERE v.v_id_experiencias <> s.s_id_experiencias
    UNION ALL
    -- R18 ---------------------------------------------------------
    SELECT 'R18', 'Cupos totales de la sesion <= capacidad maxima de su experiencia', COUNT(*)
    FROM sesion s JOIN experiencia e ON e.ex_id_experiencias = s.s_id_experiencias
    WHERE s.s_cupos_totales > e.ex_capacidad_maxima
    UNION ALL
    -- R19 ---------------------------------------------------------
    SELECT 'R19', 'Las sesiones PROGRAMADAS son solo de experiencias ACTIVAS', COUNT(*)
    FROM sesion s JOIN experiencia e ON e.ex_id_experiencias = s.s_id_experiencias
    WHERE s.s_estado = 'PROGRAMADA' AND e.ex_estado <> 'ACTIVA'
)
ORDER BY regla
"""


# ======================================================================================
# 8. PANTALLAS DEL ADMINISTRADOR
# ======================================================================================
ROJO = "#C1121F"


def _barras(df, cat, val, horizontal=False, formato="$~s", alto=300):
    if df.empty:
        st.caption("Sin datos para este periodo.")
        return
    df = df.copy()
    df["Valor"] = df[val].map(cop) if formato.startswith("$") else df[val].map(lambda x: f"{int(x):,}".replace(",", "."))
    if horizontal:
        ch = alt.Chart(df).mark_bar(color=ROJO).encode(y=alt.Y(f"{cat}:N", sort="-x", title=None, axis=alt.Axis(labelLimit=280)),
                                                       x=alt.X(f"{val}:Q", title=None, axis=alt.Axis(format=formato)),
                                                       tooltip=[alt.Tooltip(f"{cat}:N", title="Categoría"), alt.Tooltip("Valor:N", title="Valor")])
    else:
        ch = alt.Chart(df).mark_bar(color=ROJO).encode(x=alt.X(f"{cat}:N", sort=None, title=None, axis=alt.Axis(labelAngle=-40, labelLimit=160)),
                                                       y=alt.Y(f"{val}:Q", title=None, axis=alt.Axis(format=formato)),
                                                       tooltip=[alt.Tooltip(f"{cat}:N", title="Categoría"), alt.Tooltip("Valor:N", title="Valor")])
    st.altair_chart(ch.properties(height=alto), width="stretch")


def _hoy():
    return ahora().date()


# ------------------------------------------------------------------ PANEL
def panel():
    nav()
    exigir("admin")
    titulo("Administración", "Panel de control", "Cifras calculadas en Oracle sobre las ventas no canceladas, según la fecha de la sesión.")
    hoy = _hoy()
    per = st.segmented_control("Periodo", ["Todo", "Últimos 7 días", "Últimos 30 días", "Próximos 14 días"], default="Todo", key="p_per") or "Todo"
    d1, d2 = {"Todo": (date(1900, 1, 1), date(2999, 12, 31)), "Últimos 7 días": (hoy - timedelta(days=7), hoy),
              "Últimos 30 días": (hoy - timedelta(days=30), hoy), "Próximos 14 días": (hoy, hoy + timedelta(days=14))}[per]
    base = "FROM venta v JOIN sesion s ON s.s_id_sesiones = v.v_id_sesiones WHERE v.v_estado <> 'CANCELADA' AND s.s_fecha BETWEEN :d1 AND :d2"
    k = fila(f"SELECT COUNT(*) AS ventas, NVL(SUM(v.v_cantidad_personas),0) AS personas, NVL(SUM(v.v_valor_total),0) AS ingresos {base}", d1=d1, d2=d2)
    oc = fila("""SELECT NVL(SUM(s.s_cupos_totales - s.s_cupos_disponibles),0) AS vend, NVL(SUM(s.s_cupos_totales),0) AS tot
                 FROM sesion s WHERE s.s_estado <> 'CANCELADA' AND s.s_fecha BETWEEN :d1 AND :d2""", d1=d1, d2=d2)
    canc = escalar("SELECT COUNT(*) FROM venta v JOIN sesion s ON s.s_id_sesiones = v.v_id_sesiones WHERE v.v_estado = 'CANCELADA' AND s.s_fecha BETWEEN :d1 AND :d2", d1=d1, d2=d2)
    m = st.columns(5)
    m[0].metric("Ventas", f"{int(k['ventas'])}")
    m[1].metric("Personas", f"{int(k['personas'])}")
    m[2].metric("Ingresos", cop(k["ingresos"]))
    m[3].metric("Ocupación", f"{(100 * oc['vend'] / oc['tot']) if oc['tot'] else 0:.0f} %", help="Cupos vendidos / cupos ofrecidos en sesiones no canceladas.")
    m[4].metric("Ventas canceladas", int(canc))
    st.write("")
    a, b = st.columns(2, gap="large")
    with a:
        st.markdown("**Ingresos por experiencia**")
        df = consultar(f"SELECT e.ex_nombre AS exp, SUM(v.v_valor_total) AS ingresos FROM venta v JOIN sesion s ON s.s_id_sesiones = v.v_id_sesiones "
                       f"JOIN experiencia e ON e.ex_id_experiencias = v.v_id_experiencias WHERE v.v_estado <> 'CANCELADA' AND s.s_fecha BETWEEN :d1 AND :d2 "
                       "GROUP BY e.ex_nombre ORDER BY ingresos DESC", d1=d1, d2=d2)
        _barras(df, "exp", "ingresos", horizontal=True)
    with b:
        st.markdown("**Ingresos por día de sesión**")
        df = consultar(f"SELECT TO_CHAR(s.s_fecha,'DD/MM') AS dia, s.s_fecha AS f, SUM(v.v_valor_total) AS ingresos {base} GROUP BY s.s_fecha ORDER BY s.s_fecha", d1=d1, d2=d2)
        _barras(df[["dia", "ingresos"]] if not df.empty else df, "dia", "ingresos")
    a, b = st.columns(2, gap="large")
    with a:
        st.markdown("**Personas por horario**")
        df = consultar(f"SELECT s.s_hora_inicio AS hora, SUM(v.v_cantidad_personas) AS personas {base} GROUP BY s.s_hora_inicio ORDER BY s.s_hora_inicio", d1=d1, d2=d2)
        _barras(df, "hora", "personas", formato=",.0f", alto=250)
    with b:
        st.markdown("**Ventas por tipo de entrada**")
        df = consultar(f"""SELECT t.te_nombre AS tipo, COUNT(*) AS ventas, SUM(v.v_valor_total) AS ingresos FROM venta v
                           JOIN sesion s ON s.s_id_sesiones = v.v_id_sesiones JOIN tipo_experiencia t ON t.te_id_tipos_experiencia = v.v_id_tipos_experiencia
                           WHERE v.v_estado <> 'CANCELADA' AND s.s_fecha BETWEEN :d1 AND :d2 GROUP BY t.te_nombre ORDER BY ingresos DESC""", d1=d1, d2=d2)
        if df.empty:
            st.caption("Sin datos para este periodo.")
        else:
            df["tipo"] = df["tipo"].map(bonito)
            df["ingresos"] = df["ingresos"].map(cop)
            st.dataframe(df.rename(columns={"tipo": "Tipo", "ventas": "Ventas", "ingresos": "Ingresos"}), hide_index=True, width="stretch")
        st.markdown("**Ventas por estado**")
        df = consultar("SELECT v.v_estado AS estado, COUNT(*) AS ventas FROM venta v JOIN sesion s ON s.s_id_sesiones = v.v_id_sesiones "
                       "WHERE s.s_fecha BETWEEN :d1 AND :d2 GROUP BY v.v_estado ORDER BY ventas DESC", d1=d1, d2=d2)
        df["estado"] = df["estado"].map(bonito)
        st.dataframe(df.rename(columns={"estado": "Estado", "ventas": "Ventas"}), hide_index=True, width="stretch")


# ------------------------------------------------------------------ EXPERIENCIAS
def _txt(v):
    return "" if v is None or (isinstance(v, float) and v != v) else str(v)


def _form_experiencia(x, clave):
    c = st.columns([2, 1.2])
    nombre = c[0].text_input("Nombre", value=x["nombre"], key=f"{clave}_n", max_chars=100)
    tem = c[1].selectbox("Temática", TEMATICAS, index=TEMATICAS.index(x["tem"]), format_func=bonito, key=f"{clave}_t")
    desc = st.text_area("Descripción", value=_txt(x.get("desc", x.get("descr"))), max_chars=500, key=f"{clave}_d")
    c = st.columns(5)
    dur = c[0].number_input("Duración (min)", 1, 600, int(x["dur"]), key=f"{clave}_du")
    precio = c[1].number_input("Precio por persona", 0, 99999999, int(x["precio"]), step=1000, key=f"{clave}_p")
    cap = c[2].number_input("Capacidad máx.", 1, 200, int(x["cap"]), key=f"{clave}_c")
    edad = c[3].number_input("Edad mínima", 0, 99, int(x["edad"]), key=f"{clave}_e")
    est = c[4].selectbox("Estado", ["ACTIVA", "MANTENIMIENTO", "INACTIVA"], index=["ACTIVA", "MANTENIMIENTO", "INACTIVA"].index(x["est"]), format_func=bonito, key=f"{clave}_es")
    return nombre, tem, desc, dur, precio, cap, edad, est


def experiencias_admin():
    nav()
    exigir("admin")
    titulo("Administración", "Experiencias", "La cartelera es parte de la estructura del negocio: solo el administrador la modifica.")
    df = consultar("""SELECT e.ex_id_experiencias AS id, e.ex_nombre AS nombre, e.ex_tematica AS tem, e.ex_descripcion AS descr, e.ex_duracion_minutos AS dur,
                      e.ex_precio_base AS precio, e.ex_capacidad_maxima AS cap, e.ex_edad_minima AS edad, e.ex_estado AS est,
                      (SELECT COUNT(*) FROM sesion s WHERE s.s_id_experiencias = e.ex_id_experiencias) AS sesiones,
                      (SELECT COUNT(*) FROM sesion s WHERE s.s_id_experiencias = e.ex_id_experiencias AND s.s_estado = 'PROGRAMADA') AS programadas
                      FROM experiencia e ORDER BY e.ex_id_experiencias""")
    vista = pd.DataFrame({"ID": df["id"].astype(int), "Experiencia": df["nombre"], "Temática": df["tem"].map(bonito), "Min": df["dur"].astype(int),
                          "Precio": df["precio"].map(cop), "Capacidad": df["cap"].astype(int), "Edad mín.": df["edad"].astype(int), "Estado": df["est"].map(bonito),
                          "Sesiones": df["sesiones"].astype(int), "Programadas": df["programadas"].astype(int)})
    st.dataframe(vista, hide_index=True, width="stretch")
    t1, t2, t3 = st.tabs(["Editar", "Nueva experiencia", "Tipos de entrada"])
    with t1:
        ids = [int(i) for i in df["id"]]
        nombres = {int(r["id"]): r["nombre"] for r in df.to_dict("records")}
        eid = st.selectbox("Experiencia a editar", ids, format_func=lambda i: nombres[i], key="ae_sel")
        x = next(r for r in df.to_dict("records") if int(r["id"]) == eid)
        with st.form(f"ae_form_{eid}"):
            v = _form_experiencia(x, f"ae{eid}")
            if st.form_submit_button("Guardar cambios", type="primary"):
                ok, _ = intentar(editar_experiencia, eid, *v)
                if ok:
                    flash("Experiencia actualizada.")
                    st.rerun()
        st.caption("Cambiar el precio solo afecta las ventas futuras: cada venta conserva el valor unitario con el que se hizo.")
        with st.expander("Eliminar esta experiencia"):
            st.write("Solo se puede borrar si nunca tuvo sesiones. Si ya tiene historia, ponla en estado Inactiva.")
            if st.button("Eliminar definitivamente", key=f"ae_del_{eid}"):
                ok, _ = intentar(eliminar_experiencia, eid)
                if ok:
                    flash("Experiencia eliminada.")
                    st.rerun()
    with t2:
        vacio = {"nombre": "", "tem": "OTRA", "desc": "", "dur": 30, "precio": 30000, "cap": 10, "edad": 12, "est": "ACTIVA"}
        with st.form("ae_nueva"):
            v = _form_experiencia(vacio, "an")
            if st.form_submit_button("Crear experiencia", type="primary"):
                ok, _ = intentar(crear_experiencia, *v)
                if ok:
                    flash("Experiencia creada.")
                    st.rerun()
    with t3:
        tp = consultar("SELECT te_id_tipos_experiencia AS id, te_nombre AS nombre, te_descripcion AS descr, te_permite_personalizacion AS pers, "
                       "te_requiere_reserva AS reserva, te_recargo_porcentaje AS recargo FROM tipo_experiencia ORDER BY 1")
        st.dataframe(pd.DataFrame({"Tipo": tp["nombre"].map(bonito), "Descripción": tp["descr"], "Personaliza": tp["pers"], "Requiere reserva": tp["reserva"],
                                   "Recargo %": tp["recargo"]}), hide_index=True, width="stretch")
        st.info("Los tipos son solo de consulta: el recargo queda guardado en cada venta y cambiarlo con ventas existentes descuadraría la auditoría (regla R06).")
    footer()


# ------------------------------------------------------------------ SESIONES
def _sel_fila(event, df):
    rows = event.selection.rows if event and event.selection else []
    return None if not rows or rows[0] >= len(df) else df.iloc[rows[0]]


@st.dialog("Cancelar sesión")
def _dlg_cancelar_sesion(sid, n_ventas):
    st.write(f"Se cancelará la sesión **{sid}** y sus **{n_ventas}** venta(s) activa(s); los cupos quedarán libres. No se puede deshacer.")
    c = st.columns(2)
    if c[0].button("Sí, cancelar sesión", type="primary", width="stretch"):
        ok, n = intentar(cancelar_sesion, int(sid))
        if ok:
            flash(f"Sesión cancelada ({n} venta(s) canceladas y cupos devueltos).")
            st.session_state["n_ses"] = st.session_state.get("n_ses", 0) + 1
            st.rerun()
    if c[1].button("Volver", width="stretch"):
        st.rerun()


def sesiones():
    nav()
    exigir("admin")
    titulo("Administración", "Sesiones", "Programa, inicia, finaliza o cancela las sesiones de cada experiencia.")
    t1, t2 = st.tabs(["Listado y acciones", "Programar sesión"])
    exps = consultar("SELECT ex_id_experiencias AS id, ex_nombre AS nombre, ex_estado AS est, ex_capacidad_maxima AS cap FROM experiencia ORDER BY ex_nombre")
    with t1:
        hoy = _hoy()
        f = st.columns([1.6, 1.2, 1, 1])
        fe = f[0].selectbox("Experiencia", [0] + [int(i) for i in exps["id"]], format_func=lambda i: "Todas" if i == 0 else exps.set_index("id").loc[i, "nombre"])
        fs = f[1].selectbox("Estado", ["Todos", "PROGRAMADA", "EN CURSO", "FINALIZADA", "CANCELADA"], format_func=bonito)
        d1 = f[2].date_input("Desde", value=hoy - timedelta(days=2), format="DD/MM/YYYY")
        d2 = f[3].date_input("Hasta", value=hoy + timedelta(days=14), format="DD/MM/YYYY")
        df = consultar(f"""SELECT s.s_id_sesiones AS id, s.s_fecha AS fecha, s.s_hora_inicio AS hora, e.ex_nombre AS exp, s.s_zona AS zona,
                           s.s_cupos_totales AS tot, s.s_cupos_disponibles AS disp, s.s_estado AS est, s.s_id_experiencias AS eid,
                           (SELECT COUNT(*) FROM venta v WHERE v.v_id_sesiones = s.s_id_sesiones AND v.v_estado <> 'CANCELADA') AS ventas,
                           ({inicio_sql()} - SYSDATE) * 1440 AS minutos
                           FROM sesion s JOIN experiencia e ON e.ex_id_experiencias = s.s_id_experiencias
                           WHERE s.s_fecha BETWEEN :d1 AND :d2 AND (:fe = 0 OR s.s_id_experiencias = :fe) AND (:fs = 'Todos' OR s.s_estado = :fs)
                           ORDER BY s.s_fecha, s.s_hora_inicio""", d1=d1, d2=d2, fe=fe, fs=fs)
        if df.empty:
            st.info("No hay sesiones con esos filtros.")
        else:
            vista = pd.DataFrame({"ID": df["id"].astype(int), "Fecha": [fecha_larga(x) for x in df["fecha"]], "Hora": df["hora"], "Experiencia": df["exp"],
                                  "Sala": df["zona"].map(bonito), "Cupos": df["tot"].astype(int), "Libres": df["disp"].astype(int),
                                  "Ventas": df["ventas"].astype(int), "Estado": df["est"].map(bonito)})
            ev = st.dataframe(vista, hide_index=True, width="stretch", height=min(420, 38 + 35 * len(vista)), on_select="rerun", selection_mode="single-row",
                              key=f"tab_ses_{st.session_state.get('n_ses', 0)}")
            s = _sel_fila(ev, df)
            if s is None:
                st.caption("Selecciona una fila para ver las acciones disponibles.")
            else:
                sid = int(s["id"])
                with st.container(border=True):
                    st.markdown(f"**Sesión {sid}** · {e(s['exp'])} · {e(fecha_larga(s['fecha']))} {e(s['hora'])} · {pill_estado(s['est'])}", unsafe_allow_html=True)
                    a = st.columns(4)

                    def _accion(fn, msg, *args):
                        ok, _ = intentar(fn, *args)
                        if ok:
                            flash(msg)
                            st.session_state["n_ses"] = st.session_state.get("n_ses", 0) + 1
                            st.rerun()
                    if a[0].button("Iniciar sesión", disabled=s["est"] != "PROGRAMADA", key=f"si{sid}", width="stretch"):
                        _accion(iniciar_sesion, "Sesión iniciada. Ya puedes marcar la asistencia en «Ventas».", sid)
                    if a[1].button("Finalizar", disabled=s["est"] != "EN CURSO", key=f"sf{sid}", width="stretch"):
                        _accion(finalizar_sesion, "Sesión finalizada.", sid)
                    if a[2].button("Cancelar sesión", disabled=s["est"] != "PROGRAMADA", key=f"sc{sid}", width="stretch"):
                        _dlg_cancelar_sesion(sid, int(s["ventas"]))
                    if s["est"] == "PROGRAMADA":
                        with a[3].popover("Cambiar cupos", width="stretch"):
                            vendido = int(s["tot"] - s["disp"])
                            cap = int(exps.set_index("id").loc[int(s["eid"]), "cap"])
                            nuevo = st.number_input("Cupos totales", max(1, vendido), cap, int(s["tot"]), key=f"sn{sid}")
                            if st.button("Guardar", key=f"sg{sid}", type="primary"):
                                _accion(cambiar_cupos_sesion, "Cupos actualizados.", sid, int(nuevo))
                    if s["est"] == "PROGRAMADA":
                        st.caption(f"Se puede iniciar desde {MINUTOS_ANTES_PARA_INICIAR} minutos antes de la hora de inicio.")
    with t2:
        activas = exps[exps["est"] == "ACTIVA"]
        if activas.empty:
            st.info("No hay experiencias activas.")
        else:
            ids = [int(i) for i in activas["id"]]
            nom = activas.set_index("id")["nombre"].to_dict()
            ee = st.selectbox("Experiencia", ids, format_func=lambda i: nom[i], key="ns_exp")
            zona_def = escalar("SELECT s_zona FROM sesion WHERE s_id_experiencias=:e ORDER BY s_fecha DESC FETCH FIRST 1 ROWS ONLY", e=ee) or ""
            cap = int(activas.set_index("id").loc[ee, "cap"])
            with st.form("ns_form"):
                c = st.columns(4)
                fecha_s = c[0].date_input("Fecha", value=_hoy() + timedelta(days=1), min_value=_hoy(), format="DD/MM/YYYY")
                hora_s = c[1].time_input("Hora de inicio", value=time(18, 0), step=300)
                zona = c[2].text_input("Sala", value=zona_def, max_chars=50, key=f"ns_z_{ee}")
                cupos = c[3].number_input("Cupos", 1, cap, cap, help=f"Máximo {cap} (capacidad de la experiencia).")
                if st.form_submit_button("Crear sesión", type="primary"):
                    ok, _ = intentar(crear_sesion, ee, fecha_s, hora_s.strftime("%H:%M"), zona, cupos)
                    if ok:
                        flash("Sesión programada.")
                        st.rerun()
            st.caption("Una sala no puede tener dos sesiones a la misma fecha y hora, y no se programa en el pasado.")
    footer()


# ------------------------------------------------------------------ VENTAS Y ASISTENCIA
@st.dialog("Cancelar venta")
def _dlg_cancelar_venta(vid, n):
    st.write(f"Se cancelará la venta **CT-{int(vid):06d}** y se devolverán **{n}** cupo(s) a la sesión.")
    c = st.columns(2)
    if c[0].button("Sí, cancelar venta", type="primary", width="stretch"):
        ok, _ = intentar(cancelar_venta, int(vid))
        if ok:
            flash("Venta cancelada y cupos devueltos.")
            st.session_state["n_ven"] = st.session_state.get("n_ven", 0) + 1
            st.rerun()
    if c[1].button("Volver", width="stretch"):
        st.rerun()


def ventas():
    nav()
    exigir("admin")
    titulo("Administración", "Ventas y asistencia", "Las ventas nacen confirmadas (pago simulado). Aquí se marca la asistencia y se cancelan.")
    hoy = _hoy()
    exps = consultar("SELECT ex_id_experiencias AS id, ex_nombre AS nombre FROM experiencia ORDER BY ex_nombre")
    f = st.columns([1.5, 1.3, 1, 1, 1.4])
    fe = f[0].selectbox("Experiencia", [0] + [int(i) for i in exps["id"]], format_func=lambda i: "Todas" if i == 0 else exps.set_index("id").loc[i, "nombre"], key="v_e")
    fs = f[1].selectbox("Estado", ["Todos", "CONFIRMADA", "ASISTIÓ", "NO ASISTIÓ", "CANCELADA"], format_func=bonito, key="v_s")
    d1 = f[2].date_input("Sesión desde", value=hoy - timedelta(days=2), format="DD/MM/YYYY", key="v_d1")
    d2 = f[3].date_input("Hasta", value=hoy + timedelta(days=14), format="DD/MM/YYYY", key="v_d2")
    q = f[4].text_input("Buscar", placeholder="Documento, nombre o código", key="v_q").strip().upper()
    qid = int("".join(ch for ch in q if ch.isdigit()) or 0) if q.startswith("CT-") else 0
    df = consultar(f"""SELECT v.v_id_ventas AS id, c.c_nombre || ' ' || c.c_apellido AS cliente, c.c_numero_documento AS doc, e.ex_nombre AS exp,
                       s.s_fecha AS fecha, s.s_hora_inicio AS hora, s.s_estado AS sest, t.te_nombre AS tipo, v.v_cantidad_personas AS n, v.v_valor_total AS total,
                       v.v_metodo_pago AS pago, v.v_estado AS est, v.v_nivel_intensidad AS inten, v.v_actores_en_vivo AS actores, v.v_efectos_especiales AS efectos,
                       v.v_motivo_celebracion AS motivo, v.v_notas AS notas
                       FROM venta v JOIN cliente c ON c.c_id_clientes = v.v_id_clientes JOIN sesion s ON s.s_id_sesiones = v.v_id_sesiones
                       JOIN experiencia e ON e.ex_id_experiencias = v.v_id_experiencias JOIN tipo_experiencia t ON t.te_id_tipos_experiencia = v.v_id_tipos_experiencia
                       WHERE s.s_fecha BETWEEN :d1 AND :d2 AND (:fe = 0 OR v.v_id_experiencias = :fe) AND (:fs = 'Todos' OR v.v_estado = :fs)
                         AND (:q IS NULL OR UPPER(c.c_numero_documento) = :q OR UPPER(c.c_nombre || ' ' || c.c_apellido) LIKE '%' || :q || '%' OR v.v_id_ventas = :qid)
                       ORDER BY s.s_fecha DESC, s.s_hora_inicio DESC, v.v_id_ventas DESC""", d1=d1, d2=d2, fe=fe, fs=fs, q=q or None, qid=qid)
    if df.empty:
        st.info("No hay ventas con esos filtros.")
    else:
        vista = pd.DataFrame({"Código": [f"CT-{int(i):06d}" for i in df["id"]], "Cliente": df["cliente"], "Documento": df["doc"], "Experiencia": df["exp"],
                              "Sesión": [f"{fecha(a)} {b}" for a, b in zip(df["fecha"], df["hora"])], "Tipo": df["tipo"].map(bonito),
                              "Pers.": df["n"].astype(int), "Total": df["total"].map(cop), "Pago": df["pago"].map(bonito), "Estado": df["est"].map(bonito)})
        if len(df) >= 1000:
            st.caption("Se muestran las primeras 1000 ventas; afina los filtros.")
        ev = st.dataframe(vista, hide_index=True, width="stretch", height=min(420, 38 + 35 * len(vista)), on_select="rerun", selection_mode="single-row",
                          key=f"tab_ven_{st.session_state.get('n_ven', 0)}")
        v = _sel_fila(ev, df)
        if v is None:
            st.caption("Selecciona una venta para marcar asistencia o cancelarla.")
        else:
            vid = int(v["id"])
            with st.container(border=True):
                det = ""
                if v["inten"]:
                    det = (f" · Personalizada: intensidad {e(bonito(v['inten']))}{', actores' if v['actores'] == 'SI' else ''}{', efectos' if v['efectos'] == 'SI' else ''}"
                           f" · {e(bonito(v['motivo']))}" + (f" · Notas: {e(v['notas'])}" if v["notas"] else ""))
                st.markdown(f"**CT-{vid:06d}** · {e(v['cliente'])} · {e(v['exp'])} · sesión {pill_estado(v['sest'])} · venta {pill_estado(v['est'])}{det}", unsafe_allow_html=True)
                a = st.columns(4)
                activa = v["est"] == "CONFIRMADA"

                def _accion(fn, msg, *args):
                    ok, _ = intentar(fn, *args)
                    if ok:
                        flash(msg)
                        st.session_state["n_ven"] = st.session_state.get("n_ven", 0) + 1
                        st.rerun()
                marca = activa and v["sest"] in ("EN CURSO", "FINALIZADA")
                if a[0].button("Asistió", disabled=not marca, key=f"va{vid}", type="primary", width="stretch"):
                    _accion(marcar_asistencia, "Asistencia registrada.", vid, True)
                if a[1].button("No asistió", disabled=not marca, key=f"vn{vid}", width="stretch"):
                    _accion(marcar_asistencia, "Inasistencia registrada.", vid, False)
                if a[2].button("Cancelar venta", disabled=not (activa and v["sest"] != "FINALIZADA"), key=f"vc{vid}", width="stretch"):
                    _dlg_cancelar_venta(vid, int(v["n"]))
                if activa and not marca:
                    st.caption("La asistencia se marca cuando la sesión está EN CURSO (iníciala desde «Sesiones»).")
    footer()


# ------------------------------------------------------------------ TAQUILLA
def taquilla():
    nav()
    exigir("admin")
    titulo("Administración", "Taquilla", "Venta de entradas generales en la puerta al VISITANTE GENERAL (sin cuenta, sin personalización).")
    visitante = escalar("SELECT c_id_clientes FROM cliente WHERE c_numero_documento = '0'")
    exps = consultar("SELECT ex_id_experiencias AS id, ex_nombre AS nombre, ex_precio_base AS precio, ex_capacidad_maxima AS cap FROM experiencia WHERE ex_estado = 'ACTIVA' ORDER BY ex_nombre")
    tipo = fila("SELECT te_id_tipos_experiencia AS id, te_recargo_porcentaje AS pct FROM tipo_experiencia WHERE te_requiere_reserva = 'NO' AND te_permite_personalizacion = 'NO'")
    if visitante is None or exps.empty or tipo is None:
        st.warning("Falta el VISITANTE GENERAL, el tipo de entrada general o experiencias activas.")
        return
    ids = [int(i) for i in exps["id"]]
    ex = {int(r["id"]): r for r in exps.to_dict("records")}
    L, R = st.columns([1.5, 1], gap="large")
    with L:
        eid = st.selectbox("Experiencia", ids, format_func=lambda i: ex[i]["nombre"], key="t_exp")
        ses = sesiones_disponibles(eid, False)
        sid = s = None
        if ses.empty:
            st.info("No hay sesiones con cupo para esta experiencia.")
        else:
            filas = {int(r["id"]): r for r in ses.to_dict("records")}
            sid = st.selectbox("Sesión", list(filas), key=f"t_ses_{eid}",
                               format_func=lambda i: f"{fecha(filas[i]['fecha'])} · {filas[i]['hora']} · {int(filas[i]['cupos'])} cupos")
            s = filas[sid]
        maxp = min(MAX_PERSONAS_COMPRA, int(ex[eid]["cap"]), int(s["cupos"]) if s is not None else 1)
        a = st.columns(2)
        n = a[0].number_input("Personas", 1, max(1, maxp), 1, key=f"t_n_{eid}_{sid}")
        pago = a[1].selectbox("Método de pago", METODOS_TAQUILLA, format_func=bonito, key="t_pago")
    with R:
        recargo, total = calcular_precio(tipo["pct"], n, ex[eid]["precio"])
        st.markdown(f'<div class="sum"><div class="eyebrow">Venta en puerta</div><h3 style="margin:.2rem 0 .8rem">{e(ex[eid]["nombre"])}</h3>'
                    f'<div class="row"><span class="muted">Personas</span><span>{int(n)}</span></div><div class="row"><span class="muted">Valor unitario</span><span>{cop(ex[eid]["precio"])}</span></div>'
                    f'<div class="tot"><span>Total</span><span>{cop(total)}</span></div></div>', unsafe_allow_html=True)
        st.write("")
        if st.button("Registrar venta", type="primary", width="stretch", disabled=s is None, key="t_ok"):
            ok, r = intentar(comprar, int(visitante), int(sid), int(tipo["id"]), int(n), pago, metodos_validos=METODOS_TAQUILLA)
            if ok:
                flash(f"Venta registrada: CT-{int(r['id']):06d} por {cop(r['total'])}.")
                st.rerun()
        st.caption("La edad mínima no se valida en taquilla: la verifica el personal en la puerta.")
    footer()


# ------------------------------------------------------------------ CLIENTES
def clientes():
    nav()
    exigir("admin")
    titulo("Administración", "Clientes", "Cuentas registradas. Un cliente con reservas activas no se puede inactivar.")
    q = st.text_input("Buscar", placeholder="Nombre, documento o correo", key="c_q").strip().upper()
    df = consultar("""SELECT c.c_id_clientes AS id, c.c_nombre || ' ' || c.c_apellido AS nombre, c.c_tipo_documento AS td, c.c_numero_documento AS doc, c.c_correo AS correo,
                      c.c_telefono AS tel, c.c_fecha_registro AS registro, c.c_estado AS est,
                      (SELECT COUNT(*) FROM venta v WHERE v.v_id_clientes = c.c_id_clientes AND v.v_estado <> 'CANCELADA') AS ventas,
                      (SELECT NVL(SUM(v.v_valor_total),0) FROM venta v WHERE v.v_id_clientes = c.c_id_clientes AND v.v_estado <> 'CANCELADA') AS gasto
                      FROM cliente c WHERE c.c_numero_documento <> '0'
                        AND (:q IS NULL OR UPPER(c.c_nombre || ' ' || c.c_apellido) LIKE '%' || :q || '%' OR UPPER(c.c_numero_documento) LIKE '%' || :q || '%' OR UPPER(c.c_correo) LIKE '%' || :q || '%')
                      ORDER BY c.c_nombre""", q=q or None)
    if df.empty:
        st.info("No hay clientes con ese criterio.")
    else:
        vista = pd.DataFrame({"ID": df["id"].astype(int), "Cliente": df["nombre"], "Documento": df["td"] + " " + df["doc"], "Correo": df["correo"], "Teléfono": df["tel"],
                              "Registro": [fecha_larga(x) for x in df["registro"]], "Ventas": df["ventas"].astype(int), "Gastado": df["gasto"].map(cop), "Estado": df["est"].map(bonito)})
        ev = st.dataframe(vista, hide_index=True, width="stretch", height=min(430, 38 + 35 * len(vista)), on_select="rerun", selection_mode="single-row",
                          key=f"tab_cli_{st.session_state.get('n_cli', 0)}")
        c = _sel_fila(ev, df)
        if c is not None:
            nuevo = "INACTIVO" if c["est"] == "ACTIVO" else "ACTIVO"
            with st.container(border=True):
                st.markdown(f"**{e(c['nombre'])}** · {e(c['td'])} {e(c['doc'])} · {pill_estado('ACTIVA' if c['est'] == 'ACTIVO' else 'INACTIVA')}", unsafe_allow_html=True)
                if st.button("Inactivar cliente" if nuevo == "INACTIVO" else "Reactivar cliente", key=f"cl{int(c['id'])}"):
                    ok, _ = intentar(cambiar_estado_cliente, int(c["id"]), nuevo)
                    if ok:
                        flash("Cliente " + ("inactivado." if nuevo == "INACTIVO" else "reactivado."))
                        st.session_state["n_cli"] = st.session_state.get("n_cli", 0) + 1
                        st.rerun()
    footer()


# ------------------------------------------------------------------ AUDITORIA
def auditoria():
    nav()
    exigir("admin")
    titulo("Administración", "Auditoría de reglas", "Cada regla del negocio se verifica contra la base de datos: todas deben dar 0 violaciones.")
    df = consultar(AUDITORIA_SQL)
    malas = df[df["violaciones"] != 0]
    m = st.columns(3)
    m[0].metric("Reglas evaluadas", len(df))
    m[1].metric("Reglas en 0 violaciones", len(df) - len(malas))
    m[2].metric("Con violaciones", len(malas))
    if malas.empty:
        st.success("La base de datos cumple las 19 reglas del negocio.")
    else:
        st.error("Hay reglas con violaciones; revisa las filas marcadas.")
    out = pd.DataFrame({"Regla": df["regla"], "Descripción": df["descripcion"], "Violaciones": df["violaciones"].astype(int),
                        "Estado": ["Cumple" if v == 0 else "VIOLADA" for v in df["violaciones"]]})
    st.dataframe(out, hide_index=True, width="stretch", height=38 + 35 * len(out))
    st.button("Volver a auditar", key="aud_btn")
    footer()

# ======================================================================================
# 9. ARRANQUE DE LA APLICACION
# ======================================================================================
def main():
    st.set_page_config(page_title="Casa del Terror", page_icon="\U0001F3DA", layout="wide", initial_sidebar_state="collapsed")
    inyectar_estilos()
    # Si la base no responde, se explica en lugar de mostrar un error tecnico
    try:
        with get_pool().acquire() as _con:
            pass
    except Exception as ex:
        st.markdown('<div class="sec"><div class="eyebrow">Casa del Terror</div><h2>No pudimos conectar con la base de datos</h2></div>', unsafe_allow_html=True)
        st.error("Revisa que Oracle est\u00e9 encendido y que los datos de .streamlit/secrets.toml (usuario, clave y dsn) sean correctos.")
        with st.expander("Detalle t\u00e9cnico"):
            st.code(str(ex))
        st.stop()
    paginas = {
        "inicio": st.Page(inicio, title="Casa del Terror", url_path="inicio", default=True),
        "exp": st.Page(experiencias, title="Experiencias", url_path="experiencias"),
        "res": st.Page(reservar, title="Reservar", url_path="reservar"),
        "ent": st.Page(entrada, title="Entrada", url_path="entrada"),
        "mis": st.Page(mis, title="Mis reservas", url_path="mis-reservas"),
        "acc": st.Page(acceso, title="Ingresar", url_path="acceso"),
        "pan": st.Page(panel, title="Panel", url_path="panel"),
        "aexp": st.Page(experiencias_admin, title="Cartelera (admin)", url_path="admin-experiencias"),
        "ses": st.Page(sesiones, title="Sesiones", url_path="sesiones"),
        "ven": st.Page(ventas, title="Ventas", url_path="ventas"),
        "taq": st.Page(taquilla, title="Taquilla", url_path="taquilla"),
        "cli": st.Page(clientes, title="Clientes", url_path="clientes"),
        "aud": st.Page(auditoria, title="Auditor\u00eda", url_path="auditoria"),
    }
    PAGES.update(paginas)
    st.navigation(list(paginas.values()), position="hidden").run()


if __name__ == "__main__":      # Streamlit ejecuta el archivo como "__main__"; al importarlo en las pruebas no arranca la interfaz
    main()
