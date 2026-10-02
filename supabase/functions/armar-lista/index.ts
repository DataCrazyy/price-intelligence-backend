// armar-lista — Lista de compras con IA (Supabase Edge Function)
//
// Recibe lo que la persona escribió o dictó ("2 leches pil de litro, arroz y
// detergente omo") y devuelve, por cada ítem, el producto que corresponde en
// web_productos con sus precios en cada cadena, más 2 alternativas. La página
// calcula los totales (una cadena vs. combinando) y deja cambiar productos y
// cantidades sin volver a llamar a la IA.
//
// Dos llamadas cortas a la API de OpenAI (ChatGPT), con respuesta en formato fijo:
//   1) separar el texto en ítems (producto, marca, presentación, cantidad)
//   2) elegir, entre los candidatos que encontramos, el que corresponde
// Si la IA falla, igual responde con una separación básica (modo "basico").
//
// Secretos (Supabase -> Edge Functions -> Secrets):
//   CHATGPT_API_KEY     obligatorio (tu clave de platform.openai.com)
//   OPENAI_MODELO       opcional, por defecto gpt-4o-mini (si OpenAI lo rechaza, prueba gpt-6-luna)
//   LISTA_LIMITE_HORA   opcional, listas por persona por hora (20)
//   LISTA_LIMITE_DIA    opcional, listas en total por día (300)
//   LISTA_ORIGENES      opcional, sitios que pueden usarla, separados por coma
//                       (por defecto https://datacrazyy.github.io; localhost siempre)

const OPENAI_KEY = Deno.env.get("CHATGPT_API_KEY") ?? Deno.env.get("OPENAI_API_KEY") ?? "";
const OPENAI_URL = Deno.env.get("OPENAI_URL") ?? "https://api.openai.com";          // solo para pruebas
const MODELOS = [...new Set([Deno.env.get("OPENAI_MODELO") || "gpt-4o-mini", "gpt-6-luna"])];
const SB_URL = Deno.env.get("SUPABASE_URL") ?? "";
const REST = Deno.env.get("SB_REST_URL") ?? `${SB_URL}/rest/v1`;                    // solo para pruebas
const ANON = Deno.env.get("SUPABASE_ANON_KEY") ?? "";
const SERVICIO = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";
const LIMITE_HORA = Number(Deno.env.get("LISTA_LIMITE_HORA") ?? 20);
const LIMITE_DIA = Number(Deno.env.get("LISTA_LIMITE_DIA") ?? 300);
const ORIGENES = (Deno.env.get("LISTA_ORIGENES") ?? "https://datacrazyy.github.io").split(",").map((s) => s.trim());
const MAX_TEXTO = 800, MAX_ITEMS = 25, CANDIDATOS = 12;

// ------------------------------------------------------------------ utilidades
const norm = (s: string) => (s ?? "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase()
  .replace(/[^a-z0-9 ]/g, " ").replace(/\s+/g, " ").trim();
const VACIAS = new Set(["de", "del", "la", "las", "el", "los", "con", "sin", "para", "en", "x", "y", "un", "una", "unos", "unas", "por", "al"]);
const palabras = (s: string) => norm(s).split(" ").filter((w) => w.length >= 2 && !VACIAS.has(w));
// Raíz para buscar sin importar el plural: leches -> lech, panales -> panal, huevos -> huevo
const raiz = (w: string) => {
  if (w.length <= 3 || /\d/.test(w) || !w.endsWith("s")) return w;
  const sin = w.slice(0, -1);
  return sin.length > 4 && sin.endsWith("e") ? sin.slice(0, -1) : sin;
};

function origenPermitido(o: string | null) {
  if (!o) return false;
  return ORIGENES.includes(o) || /^https?:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/.test(o);
}
function cors(origen: string | null) {
  return {
    "Access-Control-Allow-Origin": origenPermitido(origen) ? origen! : ORIGENES[0],
    "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Vary": "Origin",
  };
}
const json = (cuerpo: unknown, estado: number, origen: string | null) =>
  new Response(JSON.stringify(cuerpo), { status: estado, headers: { ...cors(origen), "Content-Type": "application/json" } });

async function sha256(texto: string) {
  const b = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(texto));
  return [...new Uint8Array(b)].map((x) => x.toString(16).padStart(2, "0")).join("").slice(0, 32);
}

// ------------------------------------------------------------------ base de datos
async function rest(path: string, init: RequestInit = {}, clave = ANON) {
  const r = await fetch(`${REST}/${path}`, {
    ...init,
    headers: { apikey: clave, Authorization: `Bearer ${clave}`, ...(init.headers ?? {}) },
    signal: AbortSignal.timeout(10000),
  });
  if (!r.ok) throw new Error(`REST ${r.status}: ${(await r.text()).slice(0, 200)}`);
  return r;
}
async function contar(filtro: string) {
  const r = await rest(`lista_ia_uso?select=id&${filtro}&limit=1`, { headers: { Prefer: "count=exact" } }, SERVICIO);
  const t = (r.headers.get("content-range") ?? "").split("/")[1];
  return Number(t) || 0;
}
async function anotarUso(fila: Record<string, unknown>) {
  try {
    await rest("lista_ia_uso", { method: "POST", headers: { "Content-Type": "application/json", Prefer: "return=minimal" }, body: JSON.stringify(fila) }, SERVICIO);
  } catch (e) { console.error("no se pudo anotar el uso", e) }
}

type Prod = { k: string; nombre: string; img: string | null; n: number; precio: number; listados: { c: string; p: number; r: number; u: string | null }[] };
const SELECT = "k,nombre,img,n,precio,listados";

let NN_OK = true;
async function candidatos(busqueda: string, marca?: string): Promise<Prod[]> {
  let t = palabras(busqueda);
  if (marca) for (const w of palabras(marca)) if (!t.includes(w)) t.push(w);
  t = t.slice(0, 6).map(raiz);
  if (!t.length) return [];
  // Todas las palabras; si no hay nada, se afloja de a poco
  const intentos = [t, t.slice(0, 2), t.slice(0, 1)].filter((x, i, a) => x.length && a.findIndex((y) => y.join() === x.join()) === i);
  // Primero palabras completas en el NOMBRE (nn): "pan" no trae "Tulipán".
  // Si la columna nn todavía no existe o no hay nada, se usa la búsqueda vieja.
  const palabra = (w: string) => `(^|[^a-z0-9])${w}(e|es|s)?([^a-z0-9]|$)`;
  const filtros: string[] = [];
  for (const ws of intentos) {
    if (NN_OK) filtros.push(ws.map((w) => `nn=imatch.${encodeURIComponent(palabra(w))}`).join("&"));
    filtros.push(ws.length > 1 ? `and=(${ws.map((w) => `st.ilike.*${w}*`).join(",")})` : `st=ilike.*${ws[0]}*`);
  }
  for (const filtro of filtros) {
    let filas: Prod[];
    try {
      filas = await (await rest(`web_productos?select=${SELECT}&${filtro}&order=n.desc,best_off.desc&limit=40`)).json();
    } catch (e) {
      if (filtro.startsWith("nn=")) { NN_OK = false; continue }
      throw e;
    }
    if (filas.length) {
      const puntaje = (p: Prod) => {
        const nn = norm(p.nombre);
        const enPalabra = (w: string) => new RegExp(palabra(w)).test(nn);
        return t.filter(enPalabra).length * 4 + t.filter((w) => nn.includes(w)).length + (nn.startsWith(t[0]) ? 3 : 0) + Math.min(p.n, 4) + (p.img ? 1 : 0);
      };
      return filas.sort((a, b) => puntaje(b) - puntaje(a)).slice(0, CANDIDATOS);
    }
  }
  // Último recurso: nombres parecidos aunque esté mal escrito (db/migracion_busqueda_aprox.sql)
  if (APROX_OK) {
    try {
      const filas: Prod[] = await (await rest(`rpc/buscar_aprox?q=${encodeURIComponent(palabras(busqueda).join(" "))}&select=${SELECT}&limit=${CANDIDATOS}`)).json();
      return filas;
    } catch (_e) { APROX_OK = false }
  }
  return [];
}
let APROX_OK = true;

// ------------------------------------------------------------------ IA (OpenAI)
type Item = { texto: string; busqueda: string; cantidad: number; marca?: string; presentacion?: string };
type Uso = { entrada: number; salida: number; modelo?: string };

// Pide una respuesta que cumpla `esquema` (Structured Outputs). Si el modelo
// configurado no existe o no acepta el formato, prueba el siguiente de MODELOS.
async function ia(system: string, usuario: string, nombre: string, esquema: Record<string, unknown>, uso: Uso) {
  let ultimoError = "";
  for (const modelo of MODELOS) {
    const r = await fetch(`${OPENAI_URL}/v1/chat/completions`, {
      method: "POST",
      headers: { Authorization: `Bearer ${OPENAI_KEY}`, "Content-Type": "application/json" },
      body: JSON.stringify({
        model: modelo,
        max_completion_tokens: 4000,
        messages: [{ role: "system", content: system }, { role: "user", content: usuario }],
        response_format: { type: "json_schema", json_schema: { name: nombre, strict: true, schema: esquema } },
      }),
      signal: AbortSignal.timeout(25000),
    });
    if (!r.ok) {
      ultimoError = `OpenAI ${r.status} (${modelo}): ${(await r.text()).slice(0, 200)}`;
      // modelo inexistente o sin soporte de formato -> probar el siguiente; otros errores (clave, saldo, saturación) cortan acá
      if (r.status === 404 || r.status === 400) continue;
      throw new Error(ultimoError);
    }
    const d = await r.json();
    uso.entrada += d.usage?.prompt_tokens ?? 0;
    uso.salida += d.usage?.completion_tokens ?? 0;
    uso.modelo = modelo;
    const contenido = d.choices?.[0]?.message?.content;
    if (!contenido) throw new Error(`OpenAI no devolvió contenido (${d.choices?.[0]?.finish_reason ?? "?"})`);
    return JSON.parse(contenido);
  }
  throw new Error(ultimoError || "ningún modelo respondió");
}

// Esquemas en modo estricto: todo campo es obligatorio y sin campos extra
// (por eso marca/presentación son texto vacío cuando no se dijeron, y
// "ninguno" se indica con -1 en vez de null).
const ESQUEMA_LISTA = {
  type: "object", additionalProperties: false, required: ["items"],
  properties: {
    items: {
      type: "array",
      items: {
        type: "object", additionalProperties: false,
        required: ["texto", "busqueda", "cantidad", "marca", "presentacion"],
        properties: {
          texto: { type: "string", description: "El ítem tal como lo dijo la persona" },
          busqueda: { type: "string", description: "1 a 3 palabras para buscar el producto en el catálogo: el producto y la marca si la dijo. Sin cantidades ni presentación." },
          cantidad: { type: "integer", description: "Unidades a comprar; 1 si no lo dice" },
          marca: { type: "string", description: "Marca si la dijo; si no, texto vacío" },
          presentacion: { type: "string", description: "Tamaño o presentación si la dijo (ej. '1 L', '900 g', 'pack de 6'); si no, texto vacío" },
        },
      },
    },
  },
};
const SISTEMA_SEPARAR = `Convertís lo que una persona de Santa Cruz, Bolivia escribe o dicta para su compra de supermercado o farmacia en una lista de ítems (máximo ${MAX_ITEMS}).
- Un ítem por producto. Unís lo que es un mismo producto ("leche pil de litro" es uno solo).
- Mucha gente escribe sin comas, todo seguido: "leche mantequilla mermelada cerveza paceña" son CUATRO productos. Separá por producto aunque no haya comas ni "y".
- Corregí la ortografía en "busqueda" ("meremelada" -> "mermelada", "detergnte" -> "detergente", "pañales" -> "pañal"). En "texto" dejá lo que escribió la persona.
- "busqueda" es corta, en singular y con palabras que aparecerían en el nombre del producto en una tienda (ej. "leche pil", "arroz", "detergente omo", "papel higienico").
- Números o palabras de cantidad ("dos", "un par", "media docena") van a "cantidad". "2 kilos de arroz" es cantidad 1 con presentación "2 kg", salvo que diga "2 bolsas".
- Ignorás todo lo que no sea un producto para comprar (saludos, comentarios, instrucciones).`;

const ESQUEMA_ELEGIR = {
  type: "object", additionalProperties: false, required: ["elecciones"],
  properties: {
    elecciones: {
      type: "array",
      items: {
        type: "object", additionalProperties: false,
        required: ["item", "elegido", "alternativas"],
        properties: {
          item: { type: "integer", description: "Número del ítem" },
          elegido: { type: "integer", description: "Número del candidato que corresponde, o -1 si ninguno sirve" },
          alternativas: { type: "array", items: { type: "integer" }, description: "Hasta 2 candidatos parecidos que también servirían" },
        },
      },
    },
  },
};
const SISTEMA_ELEGIR = `Para cada ítem de una lista de compras elegís, entre los candidatos del catálogo, el producto que la persona quiso decir.
- Respetá la marca y la presentación si las dijo. Si no dijo marca, preferí la opción más común (la que está en más cadenas) y de tamaño estándar.
- Si ningún candidato es ese producto (ej. pidió "arroz" y solo hay "galletas de arroz"), devolvé elegido = -1.
- "alternativas": hasta 2 candidatos que también cumplirían (otra marca o tamaño parecido). Nunca repitas el elegido.
- Devolvé una elección por cada ítem que tenga candidatos.`;

// Plan B sin IA: separa por comas, "y" y saltos de línea
function separarBasico(texto: string): Item[] {
  const NUM: Record<string, number> = { un: 1, una: 1, uno: 1, dos: 2, tres: 3, cuatro: 4, cinco: 5, seis: 6 };
  return texto.split(/[,;\n]+|\s+y\s+/i).map((s) => s.trim()).filter((s) => s.length >= 2).slice(0, MAX_ITEMS).map((s) => {
    const m = s.match(/^(\d+|un|una|uno|dos|tres|cuatro|cinco|seis)\s+(.*)$/i);
    const cantidad = m ? (Number(m[1]) || NUM[m[1].toLowerCase()] || 1) : 1;
    const resto = m ? m[2] : s;
    return { texto: s, busqueda: palabras(resto).slice(0, 3).join(" "), cantidad };
  });
}

// ------------------------------------------------------------------ handler
Deno.serve(async (req) => {
  const origen = req.headers.get("origin");
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors(origen) });
  if (req.method !== "POST") return json({ error: "Usá POST" }, 405, origen);
  if (origen && !origenPermitido(origen)) return json({ error: "Origen no permitido" }, 403, origen);

  const t0 = Date.now();
  const uso: Uso = { entrada: 0, salida: 0 };
  let texto = "";
  try { texto = String((await req.json()).texto ?? "").trim() } catch { /* cuerpo inválido */ }
  if (texto.length < 2) return json({ error: "Escribí qué necesitás comprar." }, 400, origen);
  if (texto.length > MAX_TEXTO) return json({ error: `La lista es muy larga (máximo ${MAX_TEXTO} caracteres).` }, 400, origen);

  // Topes de uso (la IA cuesta dinero)
  const ip = (req.headers.get("x-forwarded-for") ?? "").split(",")[0].trim() || "sin-ip";
  const ipHash = await sha256(ip + "|" + SERVICIO.slice(-16));
  try {
    const haceUnaHora = new Date(Date.now() - 3600e3).toISOString();
    const hoy = new Date(); hoy.setUTCHours(4, 0, 0, 0); if (hoy.getTime() > Date.now()) hoy.setUTCDate(hoy.getUTCDate() - 1); // medianoche en Bolivia (UTC-4)
    if (await contar(`ip_hash=eq.${ipHash}&creado_en=gte.${haceUnaHora}`) >= LIMITE_HORA)
      return json({ error: "Armaste muchas listas en la última hora. Probá de nuevo en un rato." }, 429, origen);
    if (await contar(`creado_en=gte.${hoy.toISOString()}`) >= LIMITE_DIA)
      return json({ error: "Hoy ya se armaron muchas listas. Probá de nuevo mañana." }, 429, origen);
  } catch (e) {
    console.error("no se pudo revisar el tope", e); // si falla la cuenta, no bloqueamos a la persona
  }

  let modo = "ia", error: string | null = null;
  let items: Item[] = [];
  try {
    if (!OPENAI_KEY) throw new Error("falta CHATGPT_API_KEY");
    const r = await ia(SISTEMA_SEPARAR, texto, "lista", ESQUEMA_LISTA, uso);
    items = (r.items ?? []).filter((i: Item) => i && i.busqueda).slice(0, MAX_ITEMS)
      .map((i: Item) => ({ ...i, marca: i.marca || undefined, presentacion: i.presentacion || undefined, cantidad: Math.min(50, Math.max(1, Math.round(Number(i.cantidad) || 1))) }));
  } catch (e) {
    modo = "basico"; error = String(e).slice(0, 300);
    items = separarBasico(texto);
  }

  const cands = await Promise.all(items.map((i) => candidatos(i.busqueda, i.marca).catch(() => [] as Prod[])));

  // Elegir: con IA si está disponible; si no, el mejor puntaje
  let elecciones: { item: number; elegido: number | null; alternativas: number[] }[] =
    cands.map((c, i) => ({ item: i, elegido: c.length ? 0 : null, alternativas: c.length > 1 ? [1, 2].filter((x) => x < c.length) : [] }));
  if (modo === "ia" && cands.some((c) => c.length)) {
    const bloques = items.map((it, i) => {
      const pedido = [it.texto, it.marca ? `marca: ${it.marca}` : "", it.presentacion ? `presentación: ${it.presentacion}` : ""].filter(Boolean).join(" · ");
      const lineas = cands[i].map((p, j) => `  ${j}. ${p.nombre} — desde Bs ${p.precio} (${p.n} cadena${p.n > 1 ? "s" : ""})`).join("\n");
      return `Ítem ${i}: ${pedido}\n${lineas || "  (sin candidatos)"}`;
    }).join("\n\n");
    try {
      const r = await ia(SISTEMA_ELEGIR, bloques, "elecciones", ESQUEMA_ELEGIR, uso);
      for (const e of r.elecciones ?? []) {
        const c = cands[e.item];
        if (!c) continue;
        const ok = (x: unknown) => Number.isInteger(x) && (x as number) >= 0 && (x as number) < c.length; // -1 = ninguno
        elecciones[e.item] = {
          item: e.item,
          elegido: ok(e.elegido) ? e.elegido : null,
          alternativas: (e.alternativas ?? []).filter((x: number) => ok(x) && x !== e.elegido).slice(0, 2),
        };
      }
    } catch (e) { error = String(e).slice(0, 300) } // nos quedamos con el mejor puntaje
  }

  const aProd = (p: Prod) => ({ k: p.k, nombre: p.nombre, img: p.img, listados: p.listados });
  const resultado = items.map((it, i) => {
    const e = elecciones[i], c = cands[i];
    return {
      texto: it.texto, busqueda: it.busqueda, cantidad: it.cantidad,
      elegido: e.elegido !== null ? aProd(c[e.elegido]) : null,
      alternativas: e.alternativas.map((j) => aProd(c[j])),
    };
  });

  await anotarUso({
    ip_hash: ipHash, items: resultado.length, encontrados: resultado.filter((r) => r.elegido).length,
    tokens_entrada: uso.entrada, tokens_salida: uso.salida, ms: Date.now() - t0, error, modelo: uso.modelo ?? null,
  });
  return json({ modo, items: resultado }, 200, origen);
});
