-- =====================================================================
-- CASA DEL TERROR  |  03 - AUDITORIA DE REGLAS DEL NEGOCIO
-- Cada fila es una regla; "violaciones" debe ser 0 en todas.
-- Sirve para (1) comprobar que los datos de prueba son correctos y
-- (2) detectar si la aplicacion dejo pasar algo que no debia.
-- Ejecutar con "Ejecutar script" (F5) o como sentencia (Ctrl+Enter).
--
-- Una venta "activa" es la que NO esta CANCELADA (las canceladas
-- devuelven sus cupos y ya no ocupan la sesion).
-- Inicio de sesion = fecha + hora de inicio.
-- =====================================================================

SET DEFINE OFF;

SELECT regla, descripcion, violaciones
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
ORDER BY regla;

-- ---------------------------------------------------------------------
-- RESUMEN DE LOS DATOS DE PRUEBA (informativo)
-- ---------------------------------------------------------------------

-- Ventas e ingresos por experiencia (sin canceladas)
SELECT e.ex_nombre AS experiencia, COUNT(*) AS ventas,
       SUM(v.v_cantidad_personas) AS personas, SUM(v.v_valor_total) AS ingresos
FROM venta v JOIN experiencia e ON e.ex_id_experiencias = v.v_id_experiencias
WHERE v.v_estado <> 'CANCELADA'
GROUP BY e.ex_nombre
ORDER BY ingresos DESC;

-- Ventas por estado y tipo
SELECT t.te_nombre AS tipo, v.v_estado AS estado, COUNT(*) AS ventas
FROM venta v JOIN tipo_experiencia t ON t.te_id_tipos_experiencia = v.v_id_tipos_experiencia
GROUP BY t.te_nombre, v.v_estado
ORDER BY t.te_nombre, v.v_estado;

-- Sesiones por estado
SELECT s_estado AS estado, COUNT(*) AS sesiones FROM sesion GROUP BY s_estado ORDER BY s_estado;
