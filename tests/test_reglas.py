"""Pruebas de reglas contra Oracle real. Uso (desde la carpeta del proyecto):
   ORACLE_USER=... ORACLE_PASSWORD=... ORACLE_DSN=localhost:1521/XEPDB1 python tests/test_reglas.py
OJO: modifica datos de prueba; despues se puede volver a correr 00/01/02 para reiniciar la base."""
import os, sys, threading, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import oracledb, unicodedata
import App as A
from App import ReglaError, consultar, fila, escalar, transaccion
A.get_pool = (lambda p=[None]: p[0] or p.__setitem__(0, oracledb.create_pool(user=os.environ["ORACLE_USER"], password=os.environ["ORACLE_PASSWORD"], dsn=os.environ["ORACLE_DSN"], min=1, max=8, increment=1)) or p[0])
R = A

ok = fail = 0
def _sin(t):
    return "".join(c for c in unicodedata.normalize("NFD", t) if unicodedata.category(c) != "Mn").lower()

def check(nombre, esperado_error, f, texto=None):
    """esperado_error=True: debe lanzar ReglaError (y mencionar `texto` si se da)."""
    global ok, fail
    try:
        r = f()
        if esperado_error:
            fail += 1; print(f"  FALLA  {nombre}: debia rechazarse y paso")
        else:
            ok += 1; print(f"  ok     {nombre}")
        return r
    except ReglaError as e:
        if esperado_error and (texto is None or _sin(texto) in _sin(str(e))):
            ok += 1; print(f"  ok     {nombre}  -> {e}")
        else:
            fail += 1; print(f"  FALLA  {nombre}: {'mensaje inesperado' if esperado_error else 'se rechazo'}: {e}")
    except Exception as e:
        fail += 1; print(f"  FALLA  {nombre}: excepcion {type(e).__name__}: {e}")

def sesion(where, order="s.s_fecha, s.s_hora_inicio"):
    return fila(f"""SELECT s.s_id_sesiones AS id, s.s_id_experiencias AS eid, s.s_cupos_disponibles AS cupos, s.s_cupos_totales AS tot,
                    s.s_zona AS zona, s.s_fecha AS fecha FROM sesion s JOIN experiencia e ON e.ex_id_experiencias=s.s_id_experiencias
                    WHERE {where} ORDER BY {order} FETCH FIRST 1 ROWS ONLY""")

FUT = f"s.s_estado='PROGRAMADA' AND ({R.inicio_sql()} - SYSDATE)*24 > 30 AND e.ex_estado='ACTIVA'"
LIBRE = "NOT EXISTS (SELECT 1 FROM venta v WHERE v.v_id_sesiones=s.s_id_sesiones AND v.v_estado<>'CANCELADA')"
ADULTO = escalar("SELECT MIN(c_id_clientes) FROM cliente WHERE c_estado='ACTIVO' AND c_numero_documento<>'0' AND MONTHS_BETWEEN(SYSDATE,c_fecha_nacimiento)/12 >= 30")
TEEN = fila("SELECT c_id_clientes AS id FROM cliente WHERE c_estado='ACTIVO' AND c_numero_documento<>'0' AND MONTHS_BETWEEN(SYSDATE,c_fecha_nacimiento)/12 < 15 FETCH FIRST 1 ROWS ONLY")
INACTIVO = escalar("SELECT MIN(c_id_clientes) FROM cliente WHERE c_estado='INACTIVO'")
VISIT = escalar("SELECT c_id_clientes FROM cliente WHERE c_numero_documento='0'")
OTRO = escalar("SELECT c_id_clientes FROM cliente WHERE c_estado='ACTIVO' AND c_numero_documento<>'0' AND c_id_clientes<>:a AND MONTHS_BETWEEN(SYSDATE,c_fecha_nacimiento)/12>=30 ORDER BY 1 FETCH FIRST 1 ROWS ONLY", a=ADULTO)
print("adulto", ADULTO, "teen", TEEN, "inactivo", INACTIVO, "visitante", VISIT)

print("\n== Compra general: camino feliz y N06")
s = sesion(f"{FUT} AND s.s_cupos_disponibles >= 4 AND NOT EXISTS (SELECT 1 FROM venta v JOIN tipo_experiencia t ON t.te_id_tipos_experiencia=v.v_id_tipos_experiencia WHERE v.v_id_sesiones=s.s_id_sesiones AND v.v_estado<>'CANCELADA' AND t.te_permite_personalizacion='SI') AND e.ex_edad_minima<=18")
c0 = s["cupos"]
r = check("compra general 2 personas", False, lambda: R.comprar(ADULTO, s["id"], 1, 2, "TARJETA"))
v = fila("SELECT * FROM venta WHERE v_id_ventas=:i", i=r["id"])
check("venta nace CONFIRMADA", False, lambda: None if v["v_estado"] == "CONFIRMADA" else 1/0)
check("cupos descontados", False, lambda: None if escalar("SELECT s_cupos_disponibles FROM sesion WHERE s_id_sesiones=:s", s=s["id"]) == c0 - 2 else 1/0)
check("experiencia de la venta = la de la sesion (N07)", False, lambda: None if v["v_id_experiencias"] == s["eid"] else 1/0)
check("general sin personalizacion guardada", False, lambda: None if (v["v_nivel_intensidad"] is None and v["v_actores_en_vivo"] == "NO") else 1/0)

print("\n== N01-N05, N09-N10")
check("N03 mas de 15 personas", True, lambda: R.comprar(ADULTO, s["id"], 1, 16, "TARJETA"), "15")
check("N03 0 personas", True, lambda: R.comprar(ADULTO, s["id"], 1, 0, "TARJETA"))
CAP = int(escalar("SELECT ex_capacidad_maxima FROM experiencia WHERE ex_id_experiencias=:e", e=s["eid"]))
check("N03 mas que la capacidad de la experiencia", True, lambda: R.comprar(ADULTO, s["id"], 1, min(15, CAP + 1), "TARJETA")) if CAP < 15 else print("  (capacidad >= 15: se cubre con N03 maximo)")
check("N03 pago invalido", True, lambda: R.comprar(ADULTO, s["id"], 1, 1, "BITCOIN"))
check("N02 cliente inactivo", True, lambda: R.comprar(INACTIVO, s["id"], 1, 1, "TARJETA"), "inactiva")
sp = sesion("s.s_estado='CANCELADA'") or {}
if sp: check("N01 sesion cancelada", True, lambda: R.comprar(ADULTO, sp["id"], 1, 1, "TARJETA"), "disponible")
sf = sesion("s.s_estado='FINALIZADA'")
check("N01 sesion finalizada", True, lambda: R.comprar(ADULTO, sf["id"], 1, 1, "TARJETA"))
sm = sesion(f"s.s_estado='FINALIZADA' AND e.ex_estado<>'ACTIVA'")
if sm: check("N01 experiencia no activa", True, lambda: R.comprar(ADULTO, sm["id"], 1, 1, "TARJETA"))
check("N04 personalizar entrada general", True, lambda: R.comprar(ADULTO, s["id"], 1, 1, "TARJETA", intensidad="MEDIA"), "personalizacion")
check("N04 actores en entrada general", True, lambda: R.comprar(ADULTO, s["id"], 1, 1, "TARJETA", actores=True))
check("N09 reserva para el visitante general", True, lambda: R.comprar(VISIT, s["id"], 2, 1, "TARJETA", intensidad="SUAVE"), "registrados")
se = sesion(f"{FUT} AND e.ex_edad_minima >= 16 AND s.s_cupos_disponibles>=1 AND NOT EXISTS (SELECT 1 FROM venta v JOIN tipo_experiencia t ON t.te_id_tipos_experiencia=v.v_id_tipos_experiencia WHERE v.v_id_sesiones=s.s_id_sesiones AND v.v_estado<>'CANCELADA' AND t.te_permite_personalizacion='SI')")
if TEEN and se: check("N05 edad minima", True, lambda: R.comprar(TEEN["id"], se["id"], 1, 1, "TARJETA"), "mayores de")
check("N05 visitante general no se valida por edad", False, lambda: R.comprar(VISIT, se["id"], 1, 1, "EFECTIVO", metodos_validos=R.METODOS_TAQUILLA)) if se else None

print("\n== N08 / reserva personalizada")
sl = sesion(f"{FUT} AND ({R.inicio_sql()} - SYSDATE)*24 > 30 AND {LIBRE} AND e.ex_edad_minima<=18 AND s.s_cupos_totales>=3")
check("N10 reserva con menos de 24 h", True, lambda: R.comprar(ADULTO, sesion(f"{FUT} AND ({R.inicio_sql()} - SYSDATE)*24 < 24 AND {LIBRE}")["id"], 2, 1, "TARJETA", intensidad="SUAVE"), "anticipacion") if sesion(f"{FUT} AND ({R.inicio_sql()} - SYSDATE)*24 < 24 AND {LIBRE}") else print("  (sin sesion <24h libre; N10 se prueba abajo con SQL)")
sl72 = sesion(f"{FUT} AND ({R.inicio_sql()} - SYSDATE)*24 > 120 AND {LIBRE} AND e.ex_edad_minima<=18 AND s.s_cupos_totales>=3")
check("N04 reserva sin intensidad", True, lambda: R.comprar(ADULTO, sl72["id"], 2, 2, "TARJETA"), "intensidad")
rv = check("reserva personalizada valida", False, lambda: R.comprar(ADULTO, sl72["id"], 2, 3, "PSE", intensidad="EXTREMA", actores=True, efectos=True, motivo="CUMPLEAÑOS", notas="Hola"))
vv = fila("SELECT v_recargo, v_valor_unitario, v_cantidad_personas, v_valor_total FROM venta WHERE v_id_ventas=:i", i=rv["id"])
check("N06 recargo 20 % = 0.20 x 3 x unitario", False, lambda: None if float(vv["v_recargo"]) == round(0.2 * 3 * float(vv["v_valor_unitario"]), 2) and float(vv["v_valor_total"]) == 3 * float(vv["v_valor_unitario"]) + float(vv["v_recargo"]) else 1/0)
check("N08 sesion privada: otra compra general", True, lambda: R.comprar(OTRO, sl72["id"], 1, 1, "TARJETA"), "privada")
check("N08 sesion privada: otra reserva", True, lambda: R.comprar(OTRO, sl72["id"], 2, 1, "TARJETA", intensidad="SUAVE"), "privada")
sg = sesion(f"{FUT} AND ({R.inicio_sql()} - SYSDATE)*24 > 120 AND e.ex_edad_minima<=18 AND s.s_cupos_disponibles>=1 AND s.s_id_sesiones<>:x AND EXISTS (SELECT 1 FROM venta v WHERE v.v_id_sesiones=s.s_id_sesiones AND v.v_estado<>'CANCELADA')".replace(":x", str(sl72["id"])))
check("N08 reserva en sesion con entradas vendidas", True, lambda: R.comprar(OTRO, sg["id"], 2, 1, "TARJETA", intensidad="SUAVE"), "libre")

def sesion_en_horas(h, cupos=None):
    cupos = cupos or min(6, int(escalar("SELECT ex_capacidad_maxima FROM experiencia WHERE ex_id_experiencias=:e", e=sg["eid"])))
    sid = R.crear_sesion(sg["eid"], dt.date.today() + dt.timedelta(days=3), "08:%02d" % (50 + len(_creadas)), "SALA H%d" % h, cupos); _creadas.append(sid)
    with transaccion() as tx:
        tx.ejecutar("UPDATE sesion SET s_fecha=TRUNC(SYSDATE+:h/24), s_hora_inicio=TO_CHAR(SYSDATE+:h/24,'HH24:MI') WHERE s_id_sesiones=:s", h=h, s=sid)
    return sid
_creadas = []
s5 = sesion_en_horas(5)
check("N10 reserva con menos de 24 h", True, lambda: R.comprar(ADULTO, s5, 2, 1, "TARJETA", intensidad="SUAVE"), "anticipacion")
check("N01 entrada general a 5 h si se permite", False, lambda: R.comprar(ADULTO, s5, 1, 1, "TARJETA"))
s30 = sesion_en_horas(30)
x30 = R.comprar(ADULTO, s30, 2, 1, "PSE", intensidad="SUAVE")
check("N12 cancelar a menos de 48 h", True, lambda: R.cancelar_venta(x30["id"], cliente_id=ADULTO), "48")
check("   ...el admin si puede", False, lambda: R.cancelar_venta(x30["id"]))
with transaccion() as tx:
    sp_ = sesion_en_horas(2)
    tx.ejecutar("UPDATE sesion SET s_fecha=TRUNC(SYSDATE-1/24), s_hora_inicio=TO_CHAR(SYSDATE-1/24,'HH24:MI') WHERE s_id_sesiones=:s", s=sp_)
check("N01 sesion que ya empezo", True, lambda: R.comprar(ADULTO, sp_, 1, 1, "TARJETA"), "empezo")
print("\n== Cancelaciones N12-N14, N17")
check("N12 cliente cancela entrada general", True, lambda: R.cancelar_venta(r["id"], cliente_id=ADULTO), "generales")
check("N17 otro cliente intenta cancelar mi reserva", True, lambda: R.cancelar_venta(rv["id"], cliente_id=OTRO), "tu cuenta")
cupos_antes = escalar("SELECT s_cupos_disponibles FROM sesion WHERE s_id_sesiones=:s", s=sl72["id"])
check("N12 cliente cancela su reserva (>48 h)", False, lambda: R.cancelar_venta(rv["id"], cliente_id=ADULTO))
check("cupos devueltos al cancelar", False, lambda: None if escalar("SELECT s_cupos_disponibles FROM sesion WHERE s_id_sesiones=:s", s=sl72["id"]) == cupos_antes + 3 else 1/0)
check("N14 cancelar dos veces", True, lambda: R.cancelar_venta(rv["id"], cliente_id=ADULTO), "estado")
check("tras cancelar, la sesion vuelve a vender", False, lambda: R.comprar(OTRO, sl72["id"], 1, 1, "TARJETA"))
# reserva a menos de 48 h
r48 = fila(f"""SELECT v.v_id_ventas AS id, v.v_id_clientes AS cid FROM venta v JOIN sesion s ON s.s_id_sesiones=v.v_id_sesiones
               JOIN tipo_experiencia t ON t.te_id_tipos_experiencia=v.v_id_tipos_experiencia
               WHERE t.te_requiere_reserva='SI' AND v.v_estado='CONFIRMADA' AND s.s_estado='PROGRAMADA' AND ({R.inicio_sql()}-SYSDATE)*24 < 48 FETCH FIRST 1 ROWS ONLY""")
if r48: check("N12 cancelar a menos de 48 h", True, lambda: R.cancelar_venta(r48["id"], cliente_id=r48["cid"]), "48")
else:
    # se fuerza el caso: reserva nueva en sesion a >24 h y <48 h
    s48 = sesion(f"{FUT} AND ({R.inicio_sql()}-SYSDATE)*24 BETWEEN 25 AND 47 AND {LIBRE} AND e.ex_edad_minima<=18")
    if s48:
        x = R.comprar(ADULTO, s48["id"], 2, 1, "PSE", intensidad="SUAVE")
        check("N12 cancelar a menos de 48 h", True, lambda: R.cancelar_venta(x["id"], cliente_id=ADULTO), "48")
        R.cancelar_venta(x["id"])
    else: print("  (no hay sesion entre 24 y 48 h en los datos actuales)")
check("N13 admin cancela venta de sesion finalizada", True, lambda: R.cancelar_venta(escalar("SELECT MIN(v.v_id_ventas) FROM venta v JOIN sesion s ON s.s_id_sesiones=v.v_id_sesiones WHERE s.s_estado='FINALIZADA' AND v.v_estado='ASISTI'||UNISTR('\\00D3')")), "estado")
check("N13 admin cancela entrada general (sesion programada)", False, lambda: R.cancelar_venta(r["id"]))

print("\n== Asistencia y estados de sesion N14-N15")
CAPG = int(escalar("SELECT ex_capacidad_maxima FROM experiencia WHERE ex_id_experiencias=:e", e=sg["eid"]))
C5, C6, C3 = min(5, CAPG), min(6, CAPG), min(3, CAPG)
sx = R.crear_sesion(sg["eid"], dt.date.today() + dt.timedelta(days=1), "10:05", "SALA PRUEBA", C5)
check("N15 crear sesion con cupos > capacidad", True, lambda: R.crear_sesion(sg["eid"], dt.date.today() + dt.timedelta(days=1), "10:10", "SALA PRUEBA", 500), "capacidad")
check("N15 crear sesion en el pasado", True, lambda: R.crear_sesion(sg["eid"], dt.date.today() - dt.timedelta(days=1), "10:15", "SALA PRUEBA", 5), "futuro")
check("N15 hora invalida", True, lambda: R.crear_sesion(sg["eid"], dt.date.today() + dt.timedelta(days=1), "25:99", "SALA PRUEBA", 5), "HH:MM")
check("sala duplicada a la misma hora", True, lambda: R.crear_sesion(sg["eid"], dt.date.today() + dt.timedelta(days=1), "10:05", "SALA PRUEBA", 5), "sala")
exp_off = escalar("SELECT MIN(ex_id_experiencias) FROM experiencia WHERE ex_estado<>'ACTIVA'")
check("sesion de experiencia no activa", True, lambda: R.crear_sesion(exp_off, dt.date.today() + dt.timedelta(days=1), "10:20", "SALA PRUEBA", 5), "ACTIVAS")
check("N14 asistencia con sesion PROGRAMADA", True, lambda: R.marcar_asistencia(r["id"], True))
w = R.comprar(ADULTO, sx, 1, 2, "TARJETA")
check("finalizar sin estar EN CURSO", True, lambda: R.finalizar_sesion(sx), "EN CURSO")
check("iniciar sesion lejana (>1 h)", True, lambda: R.iniciar_sesion(sesion(f"{FUT} AND ({R.inicio_sql()}-SYSDATE)*24>5")["id"]), "temprano")
# forzar que la sesion de prueba quede a punto de empezar para poder iniciarla
with transaccion() as tx:
    tx.ejecutar("UPDATE sesion SET s_fecha=TRUNC(SYSDATE), s_hora_inicio=TO_CHAR(SYSDATE+10/1440,'HH24:MI') WHERE s_id_sesiones=:s", s=sx)
check("iniciar sesion proxima", False, lambda: R.iniciar_sesion(sx))
check("N15 finalizar con asistencia pendiente", True, lambda: R.finalizar_sesion(sx), "asistencia")
check("N15 cancelar sesion EN CURSO", True, lambda: R.cancelar_sesion(sx))
check("marcar asistio", False, lambda: R.marcar_asistencia(w["id"], True))
check("N14 marcar asistencia dos veces", True, lambda: R.marcar_asistencia(w["id"], False))
check("finalizar sesion", False, lambda: R.finalizar_sesion(sx))
check("N13 cancelar venta de sesion finalizada", True, lambda: R.cancelar_venta(w["id"]))

print("\n== Cancelar sesion con ventas (N15)")
sc = R.crear_sesion(sg["eid"], dt.date.today() + dt.timedelta(days=2), "11:00", "SALA PRUEBA", C6)
R.comprar(ADULTO, sc, 1, 3, "TARJETA"); R.comprar(OTRO, sc, 1, 2, "PSE")
check("cancelar sesion cancela ventas", False, lambda: R.cancelar_sesion(sc))
check("   ...y devuelve cupos", False, lambda: None if fila("SELECT s_cupos_disponibles AS d, s_cupos_totales AS t, s_estado AS e FROM sesion WHERE s_id_sesiones=:s", s=sc) == {"d": C6, "t": C6, "e": "CANCELADA"} else 1/0)
sc2 = R.crear_sesion(sg["eid"], dt.date.today() + dt.timedelta(days=2), "12:00", "SALA PRUEBA", C6)
R.comprar(ADULTO, sc2, 1, 4, "TARJETA")
check("cupos menores a lo vendido", True, lambda: R.cambiar_cupos_sesion(sc2, 3), "vendido")
check("cupos mayores a la capacidad", True, lambda: R.cambiar_cupos_sesion(sc2, 9999), "capacidad")
check("cambiar cupos valido", False, lambda: R.cambiar_cupos_sesion(sc2, CAPG))
check("   ...cupos disponibles coherentes", False, lambda: None if escalar("SELECT s_cupos_disponibles FROM sesion WHERE s_id_sesiones=:s", s=sc2) == CAPG - 4 else 1/0)

print("\n== Experiencias y clientes (N16)")
ne = check("crear experiencia", False, lambda: R.crear_experiencia("Prueba Nueva", "OTRA", "x", 10, 15000, 6, 10))
check("nombre repetido", True, lambda: R.crear_experiencia("Prueba Nueva", "OTRA", "x", 10, 15000, 6, 10), "nombre")
check("precio negativo", True, lambda: R.crear_experiencia("Otra", "OTRA", "x", 10, -5, 6, 10))
check("tematica invalida", True, lambda: R.crear_experiencia("Otra", "ALIENS", "x", 10, 5, 6, 10))
check("eliminar experiencia sin sesiones", False, lambda: R.eliminar_experiencia(ne))
check("N16 eliminar experiencia con sesiones", True, lambda: R.eliminar_experiencia(sg["eid"]), "sesiones")
XN = fila("SELECT ex_nombre AS n, ex_capacidad_maxima AS cap, ex_edad_minima AS edad, ex_duracion_minutos AS du, ex_precio_base AS p FROM experiencia WHERE ex_id_experiencias=:e", e=sg["eid"])
check("inactivar experiencia con sesiones programadas", True, lambda: R.editar_experiencia(sg["eid"], XN["n"], "OTRA", None, XN["du"], XN["p"], XN["cap"], XN["edad"], "INACTIVA"), "programada")
check("subir edad minima que rompe ventas previas", True, lambda: R.editar_experiencia(sg["eid"], XN["n"], "OTRA", None, XN["du"], XN["p"], XN["cap"], 99, "ACTIVA"), "edad minima")
check("bajar capacidad por debajo de lo programado", True, lambda: R.editar_experiencia(sg["eid"], XN["n"], "OTRA", None, XN["du"], XN["p"], 1, XN["edad"], "ACTIVA"), "capacidad")
check("editar experiencia valida (precio)", False, lambda: R.editar_experiencia(sg["eid"], XN["n"], "OTRA", None, XN["du"], float(XN["p"]) + 1000, XN["cap"], XN["edad"], "ACTIVA"))
R.editar_experiencia(sg["eid"], XN["n"], "OTRA", None, XN["du"], XN["p"], XN["cap"], XN["edad"], "ACTIVA")
check("N16 inactivar el visitante general", True, lambda: R.cambiar_estado_cliente(VISIT, "INACTIVO"), "visitante")
con_venta = escalar(f"SELECT MIN(v.v_id_clientes) FROM venta v JOIN sesion s ON s.s_id_sesiones=v.v_id_sesiones WHERE v.v_estado<>'CANCELADA' AND s.s_estado='PROGRAMADA' AND v.v_id_clientes<>:v", v=VISIT)
check("inactivar cliente con ventas programadas", True, lambda: R.cambiar_estado_cliente(con_venta, "INACTIVO"), "activa")
nc = check("registrar cliente", False, lambda: R.registrar_cliente("CC", "99887766", "Ana", "Prueba", "ana.prueba@example.com", "3001234567", dt.date(2000, 1, 1)))
check("documento repetido", True, lambda: R.registrar_cliente("CC", "99887766", "Ana", "P", "otra@example.com", None, dt.date(2000, 1, 1)), "documento")
check("correo repetido", True, lambda: R.registrar_cliente("CC", "55443322", "Ana", "P", "ana.prueba@example.com", None, dt.date(2000, 1, 1)), "correo")
check("correo invalido", True, lambda: R.registrar_cliente("CC", "55443322", "Ana", "P", "sin-arroba", None, dt.date(2000, 1, 1)), "correo")
check("sin fecha de nacimiento", True, lambda: R.registrar_cliente("CC", "55443322", "Ana", "P", "a@b.co", None, None), "nacimiento")
check("documento 0 reservado", True, lambda: R.registrar_cliente("CC", "0", "Ana", "P", "z@b.co", None, dt.date(2000, 1, 1)))
check("nacimiento en el futuro", True, lambda: R.registrar_cliente("CC", "55443322", "Ana", "P", "z@b.co", None, dt.date.today() + dt.timedelta(days=5)))
check("login correcto", False, lambda: R.autenticar_cliente("99887766", "ANA.PRUEBA@example.com"))
check("login con correo ajeno", True, lambda: R.autenticar_cliente("99887766", "juan.gomez@example.com"))
check("login del visitante general", True, lambda: R.autenticar_cliente("0", ""))
check("login cliente inactivo", True, lambda: R.autenticar_cliente(escalar("SELECT c_numero_documento FROM cliente WHERE c_id_clientes=:i", i=INACTIVO), (escalar("SELECT c_correo FROM cliente WHERE c_id_clientes=:i", i=INACTIVO) or "x@y.co")))
check("inyeccion SQL en login", True, lambda: R.autenticar_cliente("' OR '1'='1", "' OR '1'='1"))
check("actualizar contacto", False, lambda: R.actualizar_datos_cliente(nc, "ana.nueva@example.com", "3110000000"))

print("\n== Concurrencia: ultimos cupos con 2 hilos")
sk = R.crear_sesion(sg["eid"], dt.date.today() + dt.timedelta(days=3), "09:00", "SALA PRUEBA", C3)
res = []
bar = threading.Barrier(2)
def hilo(cid):
    bar.wait()
    try: R.comprar(cid, sk, 1, C3, "TARJETA"); res.append("compro")
    except ReglaError as e: res.append("rechazado: " + str(e))
ts = [threading.Thread(target=hilo, args=(c,)) for c in (ADULTO, OTRO)]
[t.start() for t in ts]; [t.join() for t in ts]
print("  resultados:", res)
check("solo una compra pasa y no hay sobreventa", False, lambda: None if sorted(r_.split(":")[0] for r_ in res) == ["compro", "rechazado"] and escalar("SELECT s_cupos_disponibles FROM sesion WHERE s_id_sesiones=:s", s=sk) == 0 else 1/0)
res.clear()
sk2 = R.crear_sesion(sg["eid"], dt.date.today() + dt.timedelta(days=3), "09:30", "SALA PRUEBA", CAPG)
bar2 = threading.Barrier(2)
def hilo2(cid, tipo):
    bar2.wait()
    try:
        R.comprar(cid, sk2, tipo, 1, "TARJETA", **({"intensidad": "SUAVE"} if tipo == 2 else {})); res.append("compro")
    except ReglaError as e: res.append("rechazado")
ts = [threading.Thread(target=hilo2, args=(ADULTO, 2)), threading.Thread(target=hilo2, args=(OTRO, 1))]
[t.start() for t in ts]; [t.join() for t in ts]
print("  resultados:", res)
check("reserva privada vs entrada general simultaneas: no conviven", False, lambda: None if res.count("compro") == 1 else 1/0)

print("\n== AUDITORIA (19 reglas)")
import re as _re
sql = A.AUDITORIA_SQL
df = consultar(sql)
print(df.to_string(index=False))
malas = df[df["violaciones"] != 0]
check("19 reglas con 0 violaciones", False, lambda: None if len(df) == 19 and malas.empty else 1/0)
print(f"\nRESULTADO: {ok} ok, {fail} fallas")
sys.exit(1 if fail else 0)
