-- =====================================================================
-- CASA DEL TERROR  |  00 - LIMPIAR (opcional)
-- Borra SOLO las 5 tablas del proyecto para poder volver a empezar.
-- No toca las tablas de la base academica (profesor, estudiante, etc.).
-- Ejecutar con "Ejecutar script" (F5). Es seguro si las tablas no existen.
-- =====================================================================

SET DEFINE OFF;

BEGIN
    FOR t IN (SELECT column_value AS nombre
              FROM TABLE(sys.odcivarchar2list('VENTA','SESION','EXPERIENCIA','CLIENTE','TIPO_EXPERIENCIA')))
    LOOP
        BEGIN
            EXECUTE IMMEDIATE 'DROP TABLE ' || t.nombre || ' CASCADE CONSTRAINTS PURGE';
            DBMS_OUTPUT.PUT_LINE('Eliminada: ' || t.nombre);
        EXCEPTION
            WHEN OTHERS THEN
                IF SQLCODE != -942 THEN RAISE; END IF;   -- -942 = la tabla no existe
        END;
    END LOOP;
END;
/
