"use client";

import { useState, useEffect } from "react";
import { useParams, useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { ArrowLeft, CheckCircle, XCircle } from "lucide-react";
import Link from "next/link";

export default function ReviewDetailPage() {
  const params = useParams();
  const router = useRouter();
  const reviewId = params.id as string;
  const [item, setItem] = useState<any>(null);
  const [notes, setNotes] = useState("");
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    api.review.get(reviewId).then((data) => {
      setItem(data);
      setNotes(data.review_notes || "");
      setLoading(false);
    }).catch(() => setLoading(false));
  }, [reviewId]);

  const handleSubmit = async (status: string) => {
    setSubmitting(true);
    try {
      await api.review.submit(reviewId, { status, notes });
      router.push("/review");
    } catch (e: any) {
      alert(e.message);
    } finally {
      setSubmitting(false);
    }
  };

  if (loading) return <div className="p-12 text-center text-gray-400 dark:text-gray-500">Loading...</div>;
  if (!item) return <div className="p-12 text-center text-gray-400 dark:text-gray-500">Review item not found</div>;

  return (
    <div>
      <Link href="/review" className="flex items-center gap-2 text-sm text-gray-500 dark:text-gray-400 dark:text-gray-500 hover:text-gray-700 dark:hover:text-gray-300 mb-4">
        <ArrowLeft className="w-4 h-4" /> Back to Review Queue
      </Link>

      <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100 mb-6">Review Detail</h1>

      <div className="grid grid-cols-2 gap-6">
        <div className="bg-white dark:bg-surface rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 p-6">
          <h2 className="text-sm font-semibold text-gray-500 dark:text-gray-400 dark:text-gray-500 uppercase mb-4">Information</h2>
          <dl className="space-y-3">
            <div><dt className="text-xs text-gray-400 dark:text-gray-500">Study UID</dt><dd className="text-sm font-mono">{item.study_instance_uid}</dd></div>
            <div><dt className="text-xs text-gray-400 dark:text-gray-500">Use Case</dt><dd className="text-sm">{item.usecase_name.replace(/_/g, " ")}</dd></div>
            <div><dt className="text-xs text-gray-400 dark:text-gray-500">Confidence Score</dt><dd className="text-sm font-bold text-amber-600">{(item.confidence_score * 100).toFixed(1)}%</dd></div>
            <div><dt className="text-xs text-gray-400 dark:text-gray-500">Status</dt><dd className="text-sm"><span className={`px-2 py-0.5 text-xs rounded-full font-medium ${
              item.status === "approved" ? "bg-green-50 dark:bg-green-950 text-green-700 dark:text-green-300" :
              item.status === "rejected" ? "bg-red-50 dark:bg-red-950 text-red-700 dark:text-red-300" :
              "bg-amber-50 dark:bg-amber-950 text-amber-700 dark:text-amber-300"
            }`}>{item.status}</span></dd></div>
            <div><dt className="text-xs text-gray-400 dark:text-gray-500">Created</dt><dd className="text-sm text-gray-600 dark:text-gray-400 dark:text-gray-500">{formatDateTime(item.created_at)}</dd></div>
            {item.reviewer && <div><dt className="text-xs text-gray-400 dark:text-gray-500">Reviewer</dt><dd className="text-sm text-gray-600 dark:text-gray-400 dark:text-gray-500">{item.reviewer}</dd></div>}
          </dl>

          <div className="mt-4">
            <Link href={`/study/${item.study_instance_uid}`} className="text-sm text-primary-600 hover:text-primary-700 font-medium">
              View Study Details
            </Link>
          </div>
        </div>

        <div className="bg-white dark:bg-surface rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 p-6">
          <h2 className="text-sm font-semibold text-gray-500 dark:text-gray-400 dark:text-gray-500 uppercase mb-4">Review</h2>
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">Notes</label>
            <textarea
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              rows={6}
              className="w-full border border-gray-200 dark:border-gray-700 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary-500"
              placeholder="Add review notes..."
            />
          </div>

          {item.status === "pending" && (
            <div className="flex gap-3 mt-4">
              <button
                onClick={() => handleSubmit("approved")}
                disabled={submitting}
                className="flex-1 flex items-center justify-center gap-2 py-2.5 bg-green-600 text-white rounded-lg hover:bg-green-700 disabled:opacity-50 text-sm font-medium"
              >
                <CheckCircle className="w-4 h-4" /> Approve
              </button>
              <button
                onClick={() => handleSubmit("rejected")}
                disabled={submitting}
                className="flex-1 flex items-center justify-center gap-2 py-2.5 bg-red-600 text-white rounded-lg hover:bg-red-700 disabled:opacity-50 text-sm font-medium"
              >
                <XCircle className="w-4 h-4" /> Reject
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
