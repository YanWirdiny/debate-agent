import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import "./index.css";
import { LiveDemo, RecordedDemo } from "./site/Demo";
import { Help } from "./site/Help";
import { Home } from "./site/Home";
import { Nav } from "./site/Nav";
import { Report } from "./views/Report";

function ReportPage() {
  return (
    <>
      <Nav />
      <Report />
    </>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Home />} />
        <Route path="/demo" element={<RecordedDemo />} />
        <Route path="/demo/live" element={<LiveDemo />} />
        <Route path="/operator" element={<Navigate to="/demo/live" replace />} />
        <Route path="/help" element={<Help />} />
        <Route path="/reports/:id" element={<ReportPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  </StrictMode>,
);
