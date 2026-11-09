// license:GPLv3+

#pragma once

#include <filesystem>
#include <string>
#include <vector>

class PinTable;
class IEditable;

// Parts locked for Claude: the MCP server refuses to change them (set, transform, delete, points, mesh, order, wireform, sheet...). Only the user locks and unlocks, in
// the editor. A part is locked when its own name is locked, when a part group it is in is locked, or when it is a member of a locked collection.
// Saved next to the table (<table>.locks.json), by name.
namespace VPX::Locks
{

class Store
{
public:
   // The store of a table (the live copy of a playtest shares the store of its base table). Loaded from the sidecar file on first use.
   static Store& For(const PinTable* table);

   bool Empty() const { return m_parts.empty() && m_collections.empty(); }
   const std::vector<std::string>& Parts() const { return m_parts; } // part and part group names
   const std::vector<std::string>& Collections() const { return m_collections; }
   uint64_t Seq() const { return m_seq; }
   const std::filesystem::path& Path() const { return m_path; }

   bool IsPartNameLocked(const std::string& name) const;
   bool IsCollectionNameLocked(const std::string& name) const;
   // Locks or unlocks a part (or part group) / a collection by name
   void SetPart(const std::string& name, bool locked);
   void SetCollection(const std::string& name, bool locked);

   // Why a part is locked, empty when it is not: "the part itself", "its part group 'G'", "its collection 'C'"
   std::string Reason(const IEditable* part, const PinTable* table) const;

private:
   void Load();
   void Save();

   std::filesystem::path m_path;
   std::vector<std::string> m_parts;
   std::vector<std::string> m_collections;
   uint64_t m_seq = 0;
};

}
