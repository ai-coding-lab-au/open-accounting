import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import type { OutgoingDocument } from "../types/api";
import OutgoingContent from "../components/outgoing/OutgoingContent";
import OutgoingCreateDialog from "../components/outgoing/OutgoingCreateDialog";
import OutgoingDetailDrawer from "../components/outgoing/OutgoingDetailDrawer";

export default function DocumentsPage() {
  const qc = useQueryClient();
  const [selectedOutgoing, setSelectedOutgoing] = useState<OutgoingDocument | null>(null);
  const [creatingReceipt, setCreatingReceipt] = useState(false);

  return (
    <div className="space-y-6">
      <section className="space-y-5">
        <OutgoingContent
          title="Receipts"
          fixedDocType="receipt"
          onSelectDocument={setSelectedOutgoing}
          createActions={[
            {
              docType: "receipt",
              label: "+ Receipt",
              className: "btn-primary",
              onClick: () => setCreatingReceipt(true),
            },
          ]}
        />
      </section>

      {selectedOutgoing && (
        <OutgoingDetailDrawer
          doc={selectedOutgoing}
          onClose={() => {
            setSelectedOutgoing(null);
            qc.invalidateQueries({ queryKey: ["outgoing"] });
          }}
        />
      )}

      {creatingReceipt && (
        <OutgoingCreateDialog
          docType="receipt"
          onClose={() => setCreatingReceipt(false)}
          onCreated={(doc) => {
            setCreatingReceipt(false);
            setSelectedOutgoing(doc);
          }}
        />
      )}
    </div>
  );
}
