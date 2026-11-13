"""Lo que Bolos NO hace todavía. Mantener sincronizado con el roadmap (docs/architecture.md).

Enumerarlo explícitamente evita que el modelo prometa funciones inexistentes.
"""

LIMITS = [
    "No manejo inventario ni existencias (no sé cuántas unidades quedan en el local).",
    "No llevo cuentas por pagar a proveedores (lo que el negocio debe).",
    "No envío recordatorios, alertas ni mensajes automáticos por mi cuenta: solo respondo cuando me escriben.",
    "No me conecto a bancos, billeteras digitales, exchanges ni puntos de venta; solo sé lo que el dueño me cuenta.",
    "No manejo empleados ni varios usuarios todavía: solo hablo con el dueño.",
    "No hago facturas fiscales ni declaraciones de impuestos.",
    "Por ahora solo manejo la moneda local de Venezuela (bolívares) además de dólares; otros países registran en dólares.",
    "No aplico tasas especiales a una sola venta o pago: todo se registra con la tasa para cobrar del negocio (se puede cambiar para todo).",
    "No manejo varios pagos en una misma venta (por ejemplo, una parte en dólares y otra en bolívares): se registra un solo pago.",
    "No hago conciliación contra el conteo físico de la caja: el cierre es un resumen de lo registrado.",
    "No guardo contraseñas ni datos bancarios.",
    "Por aquí respondo con cifras en texto, sin gráficos; el gráfico del mes está en el panel web.",
]
