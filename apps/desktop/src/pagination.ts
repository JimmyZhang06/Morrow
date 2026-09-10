type Page<T> = { items: T[]; next_cursor: string | null };
type Result<T> = { ok: boolean; status: number; data: T; headers: { etag?: string | null; contentType?: string | null } };

// Publish a history snapshot only after every page has succeeded.
export async function collectPages<T>(
  fetchPage: (cursor: string | null) => Promise<Result<Page<T>>>,
): Promise<Result<Page<T>>> {
  const items: T[] = [];
  const seen = new Set<string>();
  let cursor: string | null = null;
  while (true) {
    const result = await fetchPage(cursor);
    if (!result.ok) return result;
    const page = result.data;
    if (!page || !Array.isArray(page.items)
      || (page.next_cursor !== null && typeof page.next_cursor !== "string")
      || (page.next_cursor !== null && (!page.next_cursor || seen.has(page.next_cursor)))) {
      return { ok: false, status: 502, data: { items: [], next_cursor: null }, headers: {} };
    }
    items.push(...page.items);
    cursor = page.next_cursor;
    if (cursor === null) return { ...result, data: { items, next_cursor: null } };
    seen.add(cursor);
  }
}
