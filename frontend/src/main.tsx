import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import "./index.css";
import { Help } from "./site/Help";
import { Home } from "./site/Home";
import { Nav } from "./site/Nav";
import { Operator } from "./views/Operator";
import { Report } from "./views/Report";

/** Demo = the operator console under the site nav (the presentation's live part). */
function Demo() {
  return (
    <>
      <Nav />
      <Operator />
    </>
  );
}

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
        <Route path="/demo" element={<Demo />} />
        <Route path="/operator" element={<Navigate to="/demo" replace />} />
        <Route path="/help" element={<Help />} />
        <Route path="/reports/:id" element={<ReportPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  </StrictMode>,
);
