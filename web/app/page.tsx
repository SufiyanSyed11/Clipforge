import Link from "next/link";

export default function Home() {
  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold">Projects</h1>
      <p className="text-sm text-gray-400">
        Project creation arrives in Phase 1. Use the{" "}
        <Link href="/system" className="text-accent underline">
          System
        </Link>{" "}
        page to check the end-to-end pipeline and this PC&apos;s hardware.
      </p>
    </div>
  );
}
