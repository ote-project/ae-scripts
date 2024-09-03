require_relative 'schema_extractor'

parser = SchemaParser.new
schema = parser.parse('/home/ubuntu/dse/diaspora/db/schema.rb')

# Print out the parsed schema
#schema.each do |table_name, table_info|
#  puts "Table: #{table_name}"
#  puts "Columns:"
#  table_info[:columns].each do |column_name, column_info|
#    puts "  #{column_name}: #{column_info[:type]} #{column_info[:options]}"
#  end
#  puts "Indices:"
#  table_info[:indices].each do |index|
#    puts "  #{index[:columns]} #{index[:options]}"
#  end
#  puts "Foreign Keys:"
#  table_info[:foreign_keys].each do |fk|
#    puts "  to #{fk[:to_table]} #{fk[:options]}"
#  end
#  puts "\n"
#end
