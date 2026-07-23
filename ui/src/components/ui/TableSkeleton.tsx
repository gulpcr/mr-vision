interface RowProps {
  columnWidths: number[];
  subtextColumns?: number[];
}

function SkeletonRow({ columnWidths, subtextColumns = [] }: RowProps) {
  return (
    <tr className="border-b border-gray-50 dark:border-gray-800">
      {columnWidths.map((w, i) => (
        <td key={i} className="py-4 px-4">
          <div
            className="h-4 bg-gray-100 dark:bg-gray-800 rounded animate-pulse motion-reduce:animate-none"
            style={{ width: w }}
          />
          {subtextColumns.includes(i) && (
            <div
              className="h-3 bg-gray-100 dark:bg-gray-800 rounded animate-pulse motion-reduce:animate-none mt-1.5"
              style={{ width: w * 0.55 }}
            />
          )}
        </td>
      ))}
    </tr>
  );
}

interface TableSkeletonProps extends RowProps {
  rows?: number;
}

/** Skeleton rows matching a real table's column shape — for use inside a <tbody>. */
export function TableSkeleton({ rows = 7, columnWidths, subtextColumns }: TableSkeletonProps) {
  return (
    <>
      {Array.from({ length: rows }).map((_, i) => (
        <SkeletonRow key={i} columnWidths={columnWidths} subtextColumns={subtextColumns} />
      ))}
    </>
  );
}
